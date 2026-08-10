# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-neutral credential-field and credential-value classification."""

from __future__ import annotations

import re
from typing import Any, Mapping
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


class SecretFieldError(ValueError):
    """A decoded value contains credential-shaped publication data."""


def _trim_bounds(value: str) -> tuple[int, int]:
    start = 0
    end = len(value)
    while start < end and value[start].isspace():
        start += 1
    while end > start and value[end - 1].isspace():
        end -= 1
    return start, end


def _authority_contains_userinfo(
    value: str,
    start: int,
    end: int,
    *,
    allow_whitespace: bool,
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
    return False


def _urlsplit_start(value: str, start: int, end: int) -> int:
    """Skip the leading WHATWG C0 controls and space stripped by urlsplit."""

    while start < end and ord(value[start]) <= 0x20:
        start += 1
    return start


def _is_standalone_url_scheme(value: str, start: int, separator: int) -> bool:
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
    return True


def _string_contains_url_credentials(value: str, start: int, end: int) -> bool:
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


def assert_no_secret_fields(value: Any, *, source: str = "value") -> None:
    """Reject decoded credential keys, URL userinfo, and auth header values."""

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if (
                    normalized in _NORMALIZED_SECRET_FIELD_NAMES
                    or normalized.endswith(_NORMALIZED_SECRET_FIELD_SUFFIXES)
                    or normalized.startswith(_NORMALIZED_SECRET_HEADER_PREFIXES)
                ):
                    raise SecretFieldError(
                        f"{source} contains a credential field or secret field: {key}"
                    )
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            start, end = _trim_bounds(current)
            if _string_contains_url_credentials(current, start, end):
                raise SecretFieldError(f"{source} must not contain URL credentials")
            prefix = current[start : min(end, start + 7)].casefold()
            if prefix.startswith(("bearer ", "basic ")):
                raise SecretFieldError(
                    f"{source} must not contain authorization credentials"
                )


__all__ = ["SecretFieldError", "assert_no_secret_fields"]
