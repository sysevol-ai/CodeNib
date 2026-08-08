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


def assert_no_secret_fields(value: Any, *, source: str = "value") -> None:
    """Reject decoded credential keys, URL userinfo, and auth header values."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if (
                normalized in _NORMALIZED_SECRET_FIELD_NAMES
                or normalized.endswith(_NORMALIZED_SECRET_FIELD_SUFFIXES)
                or normalized.startswith(_NORMALIZED_SECRET_HEADER_PREFIXES)
            ):
                raise SecretFieldError(
                    f"{source} contains a credential field or secret field: {key}"
                )
            assert_no_secret_fields(child, source=source)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_secret_fields(child, source=source)
    elif isinstance(value, str):
        stripped = value.strip()
        if "://" in stripped or stripped.startswith("//"):
            try:
                parsed = urlsplit(stripped)
            except ValueError as exc:
                raise SecretFieldError(f"{source} contains an invalid URL") from exc
            if parsed.username is not None or parsed.password is not None:
                raise SecretFieldError(f"{source} must not contain URL credentials")
        normalized = stripped.casefold()
        if normalized.startswith(("bearer ", "basic ")):
            raise SecretFieldError(
                f"{source} must not contain authorization credentials"
            )


__all__ = ["SecretFieldError", "assert_no_secret_fields"]
