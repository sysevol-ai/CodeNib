# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-neutral credential-field and credential-value classification."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

_SECRET_FIELD_NAMES = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "headers",
        "password",
        "passwd",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
        "x_api_key",
        "x_auth_token",
    }
)
_NORMALIZED_SECRET_FIELD_NAMES = frozenset(
    re.sub(r"[^a-z0-9]", "", name.casefold()) for name in _SECRET_FIELD_NAMES
)
_NORMALIZED_SECRET_FIELD_SUFFIXES = tuple(
    re.sub(r"[^a-z0-9]", "", name.casefold())
    for name in (
        "api_key",
        "access_key",
        "access_token",
        "auth_token",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
        "authorization",
    )
)
_NORMALIZED_SECRET_HEADER_PREFIXES = tuple(
    re.sub(r"[^a-z0-9]", "", name.casefold())
    for name in ("proxy_authorization", "x_api_key", "x_auth_token")
)
_STRING_SCAN_CHARS = 64 * 1024
_SECRET_KEY_LIMIT = max(
    *(len(value) for value in _NORMALIZED_SECRET_FIELD_NAMES),
    *(len(value) for value in _NORMALIZED_SECRET_FIELD_SUFFIXES),
    *(len(value) for value in _NORMALIZED_SECRET_HEADER_PREFIXES),
)


class SecretFieldError(ValueError):
    """A decoded value contains credential-shaped publication data."""


def _secret_key_matches(
    key: object,
    *,
    check_cancelled: Callable[[], None] | None,
) -> bool:
    text = str.__str__(key) if isinstance(key, str) else str(key)
    if check_cancelled is None:
        normalized = re.sub(r"[^a-z0-9]", "", text.casefold())
        return (
            normalized in _NORMALIZED_SECRET_FIELD_NAMES
            or normalized.endswith(_NORMALIZED_SECRET_FIELD_SUFFIXES)
            or normalized.startswith(_NORMALIZED_SECRET_HEADER_PREFIXES)
        )

    exact_parts: list[str] | None = []
    normalized_length = 0
    prefix = ""
    suffix = ""
    text_length = len(text)
    for offset in range(0, text_length, _STRING_SCAN_CHARS):
        end = min(text_length, offset + _STRING_SCAN_CHARS)
        piece = re.sub(r"[^a-z0-9]", "", text[offset:end].casefold())
        normalized_length += len(piece)
        if exact_parts is not None:
            if normalized_length <= _SECRET_KEY_LIMIT:
                exact_parts.append(piece)
            else:
                exact_parts = None
        if len(prefix) < _SECRET_KEY_LIMIT:
            prefix = (prefix + piece)[:_SECRET_KEY_LIMIT]
        suffix = (suffix + piece)[-_SECRET_KEY_LIMIT:]
        if prefix.startswith(_NORMALIZED_SECRET_HEADER_PREFIXES):
            return True
        if end < text_length:
            check_cancelled()
    exact = None if exact_parts is None else "".join(exact_parts)
    return (
        exact in _NORMALIZED_SECRET_FIELD_NAMES
        or suffix.endswith(_NORMALIZED_SECRET_FIELD_SUFFIXES)
        or prefix.startswith(_NORMALIZED_SECRET_HEADER_PREFIXES)
    )


def _trim_bounds(
    value: str,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[int, int]:
    start = 0
    end = len(value)
    while start < end:
        window_end = min(end, start + _STRING_SCAN_CHARS)
        for position in range(start, window_end):
            if not value[position].isspace():
                start = position
                break
        else:
            start = window_end
            if check_cancelled is not None and start < end:
                check_cancelled()
            continue
        break
    while end > start:
        window_start = max(start, end - _STRING_SCAN_CHARS)
        for position in range(end - 1, window_start - 1, -1):
            if not value[position].isspace():
                end = position + 1
                break
        else:
            end = window_start
            if check_cancelled is not None and end > start:
                check_cancelled()
            continue
        break
    return start, end


def _authority_contains_userinfo(
    value: str,
    start: int,
    end: int,
    *,
    allow_whitespace: bool,
    check_cancelled: Callable[[], None] | None = None,
) -> bool:
    """Check a URL authority without copying a potentially huge document."""

    position = start
    while position < end:
        char = value[position]
        # urllib.parse treats whitespace inside a standalone URL netloc as
        # part of the authority. Embedded URLs still stop at prose whitespace
        # so a later email address is not misclassified as URL userinfo.
        if char in "/?#" or (not allow_whitespace and char.isspace()):
            break
        if char == "@":
            return True
        position += 1
        if (
            check_cancelled is not None
            and position < end
            and position % _STRING_SCAN_CHARS == 0
        ):
            check_cancelled()
    return False


def _urlsplit_start(
    value: str,
    start: int,
    end: int,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> int:
    """Skip the leading WHATWG C0 controls and space stripped by urlsplit."""

    while start < end and ord(value[start]) <= 0x20:
        start += 1
        if (
            check_cancelled is not None
            and start < end
            and start % _STRING_SCAN_CHARS == 0
        ):
            check_cancelled()
    return start


def _is_standalone_url_scheme(
    value: str,
    start: int,
    separator: int,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> bool:
    """Return whether the first ``://`` follows a scheme at the trim boundary."""

    if separator <= start:
        return False
    first = value[start]
    if not (first.isascii() and first.isalpha()):
        return False
    for position in range(start + 1, separator):
        char = value[position]
        if not (char.isascii() and (char.isalnum() or char in "+-.")):
            return False
        if (
            check_cancelled is not None
            and position + 1 < separator
            and (position + 1 - start) % _STRING_SCAN_CHARS == 0
        ):
            check_cancelled()
    return True


def _find_scheme_separator(
    value: str,
    start: int,
    end: int,
    *,
    check_cancelled: Callable[[], None] | None,
) -> int:
    """Find the next scheme separator without one unbounded string scan."""

    position = start
    while position < end:
        window_end = min(end, position + _STRING_SCAN_CHARS + 2)
        found = value.find("://", position, window_end)
        if found >= 0:
            return found
        next_position = max(position + 1, window_end - 2)
        if next_position >= end:
            return -1
        if check_cancelled is not None:
            check_cancelled()
        position = next_position
    return -1


def _string_contains_url_credentials(
    value: str,
    start: int,
    end: int,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> bool:
    if check_cancelled is None:
        url_start = _urlsplit_start(value, start, end)
        if end - start <= 8_192:
            candidate = value[url_start:end]
            if "://" not in candidate and not candidate.startswith("//"):
                return False
            try:
                parsed = urlsplit(candidate)
            except ValueError as exc:
                raise SecretFieldError("value contains an invalid URL") from exc
            return parsed.username is not None or parsed.password is not None

        if value.startswith("//", url_start) and _authority_contains_userinfo(
            value,
            url_start + 2,
            end,
            allow_whitespace=True,
        ):
            return True
        separator = value.find("://", url_start, end)
        standalone_url = separator >= 0 and _is_standalone_url_scheme(
            value,
            url_start,
            separator,
        )
        while separator >= 0:
            if _authority_contains_userinfo(
                value,
                separator + 3,
                end,
                allow_whitespace=standalone_url,
            ):
                return True
            standalone_url = False
            separator = value.find("://", separator + 3, end)
        return False

    url_start = _urlsplit_start(
        value,
        start,
        end,
        check_cancelled=check_cancelled,
    )
    if end - start <= 8_192:
        candidate = value[url_start:end]
        if "://" not in candidate and not candidate.startswith("//"):
            return False
        try:
            parsed = urlsplit(candidate)
        except ValueError as exc:
            raise SecretFieldError("value contains an invalid URL") from exc
        return parsed.username is not None or parsed.password is not None

    if value.startswith("//", url_start):
        authority_has_userinfo = _authority_contains_userinfo(
            value,
            url_start + 2,
            end,
            allow_whitespace=True,
            check_cancelled=check_cancelled,
        )
        if authority_has_userinfo:
            return True
    separator = _find_scheme_separator(
        value,
        url_start,
        end,
        check_cancelled=check_cancelled,
    )
    standalone_url = separator >= 0 and _is_standalone_url_scheme(
        value,
        url_start,
        separator,
        check_cancelled=check_cancelled,
    )
    while separator >= 0:
        authority_has_userinfo = _authority_contains_userinfo(
            value,
            separator + 3,
            end,
            allow_whitespace=standalone_url,
            check_cancelled=check_cancelled,
        )
        if authority_has_userinfo:
            return True
        standalone_url = False
        if separator + 3 < end:
            check_cancelled()
        separator = _find_scheme_separator(
            value,
            separator + 3,
            end,
            check_cancelled=check_cancelled,
        )
    return False


def assert_no_secret_fields(
    value: Any,
    *,
    source: str = "value",
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Reject decoded credential keys, URL userinfo, and auth header values."""

    if check_cancelled is not None:
        if not callable(check_cancelled):
            raise TypeError("secret-field cancellation check must be callable")
        check_cancelled()

    def validate_current_scalar(current: Any) -> bool:
        """Validate one current leaf before polling for a future sibling."""

        if isinstance(current, (Mapping, list, tuple)):
            return False
        if isinstance(current, str):
            start, end = _trim_bounds(
                current,
                check_cancelled=check_cancelled,
            )
            if _string_contains_url_credentials(
                current,
                start,
                end,
                check_cancelled=check_cancelled,
            ):
                raise SecretFieldError(f"{source} must not contain URL credentials")
            prefix = current[start : min(end, start + 7)].casefold()
            if prefix.startswith(("bearer ", "basic ")):
                raise SecretFieldError(
                    f"{source} must not contain authorization credentials"
                )
        return True

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if check_cancelled is None or type(current) is dict:
                items = current.items()
                item_count: int | None = len(current)
                for index, (key, child) in enumerate(items):
                    if _secret_key_matches(
                        key,
                        check_cancelled=check_cancelled,
                    ):
                        raise SecretFieldError(
                            f"{source} contains a credential field or secret field: "
                            f"{key}"
                        )
                    if check_cancelled is None:
                        stack.append(child)
                    elif not validate_current_scalar(child):
                        stack.append(child)
                    if check_cancelled is not None and index + 1 < item_count:
                        check_cancelled()
            else:
                assert check_cancelled is not None
                check_cancelled()
                keys = current.keys()
                check_cancelled()
                iterator = iter(keys)
                check_cancelled()
                while True:
                    try:
                        key = next(iterator)
                    except StopIteration:
                        break
                    if _secret_key_matches(
                        key,
                        check_cancelled=check_cancelled,
                    ):
                        raise SecretFieldError(
                            f"{source} contains a credential field or secret field: "
                            f"{key}"
                        )
                    check_cancelled()
                    child = current[key]
                    if not validate_current_scalar(child):
                        stack.append(child)
                    check_cancelled()
        elif isinstance(current, (list, tuple)):
            if check_cancelled is None or type(current) in {list, tuple}:
                children = current
                item_count: int | None = len(current)
            else:
                check_cancelled()
                children = iter(current)
                check_cancelled()
                item_count = None
            for index, child in enumerate(children):
                if check_cancelled is None:
                    stack.append(child)
                elif not validate_current_scalar(child):
                    stack.append(child)
                if check_cancelled is not None and (
                    item_count is None or index + 1 < item_count
                ):
                    check_cancelled()
        else:
            validate_current_scalar(current)
        if check_cancelled is not None and stack:
            check_cancelled()


__all__ = ["SecretFieldError", "assert_no_secret_fields"]
