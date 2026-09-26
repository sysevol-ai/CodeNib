# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import pytest

from codenib._secret_fields import SecretFieldError, assert_no_secret_fields


@pytest.mark.parametrize(
    "title",
    [
        "Basic authentication adds a request header",
        "Basic and digest authentication",
        "Bearer tokens remain in the caller's agent",
    ],
)
def test_authentication_prose_is_publishable(title):
    assert_no_secret_fields({"title": title, "story": {"section": title}})


@pytest.mark.parametrize(
    "value",
    [
        "Bearer current-secret",
        "Basic dXNlcjpwYXNz",
        "  bAsIc Zm9vOmJhcg==  ",
        "BEARER\t  token-._~+/==",
        "Basic ambiguous",
    ],
)
def test_complete_authorization_values_still_block_publication(value):
    with pytest.raises(SecretFieldError, match="authorization credentials"):
        assert_no_secret_fields({"title": value})


@pytest.mark.parametrize("key", ["Authorization", "api_key", "proxy-authorization"])
def test_credential_fields_reject_prose_too(key):
    with pytest.raises(SecretFieldError, match="credential field"):
        assert_no_secret_fields({key: "Basic authentication adds a header"})


def test_long_authorization_values_remain_cancellable(monkeypatch):
    import codenib._secret_fields as fields

    monkeypatch.setattr(fields, "_STRING_SCAN_CHARS", 16)
    # Exercise this scan directly so URL/userinfo scanning cannot satisfy the
    # cancellation assertion before the token grammar is examined.
    value = "Bearer " + "x" * 100

    def cancel():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        fields._is_authorization_value(value, 0, len(value), check_cancelled=cancel)
