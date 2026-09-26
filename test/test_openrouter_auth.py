# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise real local OAuth callbacks and private storage; fake only the provider."""

from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests

from codenib import openrouter_auth as auth
from codenib.compiler.cache_lock import COMPILER_CACHE_LOCK_FILENAME

KEY = "sk-or-v1-local-fixture-secret"
GRANT = "single-use-fixture-grant"
REAL_KEYRING_RESOLVER = auth._os_keyring


@pytest.fixture(autouse=True)
def vault(monkeypatch, tmp_path):
    class Vault:
        key = None

        def get_password(self, service, account):
            assert (service, account) == ("codenib/openrouter", "default")
            return self.key

        def set_password(self, service, account, key):
            self.key = key

        def delete_password(self, service, account):
            self.key = None

    backend = Vault()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(auth, "_os_keyring", lambda: backend)
    monkeypatch.setattr(
        auth, "_credential_root", lambda: tmp_path / "state" / "credentials"
    )
    return backend


@pytest.fixture
def api(monkeypatch):
    real_send = requests.Session.send
    fake = SimpleNamespace(calls=[], status=200, management=False, override=None)

    def send(session, request, **options):
        if request.url.startswith("http://localhost:"):
            return real_send(session, request, **options)
        assert request.url in {
            "https://openrouter.ai/api/v1/key",
            "https://openrouter.ai/api/v1/auth/keys",
        }
        assert options["allow_redirects"] is False
        fake.calls.append(request)
        if request.method == "POST":
            assert "Authorization" not in request.headers
            payload = json.loads(request.body)
            assert payload["code"] == GRANT
            assert payload["code_challenge_method"] == "S256"
            body = {"key": KEY}
        else:
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            body = {
                "data": {
                    "is_management_key": fake.management,
                    "is_provisioning_key": False,
                    "limit": 5,
                    "limit_remaining": 4,
                    "usage": 1,
                    "label": KEY,
                }
            }
        response = requests.Response()
        response.status_code = fake.status
        response.url = request.url
        response._content = json.dumps(
            fake.override if fake.override is not None else body
        ).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return fake


def test_headless_pkce_exchanges_once_and_saves_only_in_os_vault(api, vault):
    announced = []
    result = auth.login(
        headless=True, read_code=lambda _: GRANT, announce=announced.append
    )
    params = parse_qs(urlsplit(announced[0]).query)
    payload = json.loads(api.calls[0].body)
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(payload["code_verifier"].encode()).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert params["code_challenge"] == [expected]
    assert params["code_challenge_method"] == ["S256"]
    assert "callback_url" not in params
    assert payload["code_verifier"] not in announced[0]
    assert vault.key == KEY
    assert result["limit"] == 5
    assert KEY not in json.dumps(result)
    assert GRANT not in json.dumps(result)
    assert not auth._credential_root().exists()
    assert auth.credential() == (KEY, "keyring")


def test_real_callback_rejects_wrong_host_state_and_duplicate_codes(api):
    announced = Queue()
    attempt = auth.LoginAttempt(timeout=5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            auth.browser_grant,
            attempt,
            announce=announced.put,
            open_browser=lambda _: None,
        )
        url = announced.get(timeout=2)
        callback = parse_qs(urlsplit(url).query)["callback_url"][0]
        for invalid_url, headers in (
            (callback.replace(attempt.nonce, "wrong-state") + "?code=" + GRANT, {}),
            (callback + "?code=" + GRANT, {"Host": "attacker.invalid"}),
            (callback + "?code=first&code=second", {}),
        ):
            response = requests.get(invalid_url, headers=headers, timeout=2)
            assert response.status_code in {400, 404}
            assert not future.done()
        response = requests.get(callback + "?" + urlencode({"code": GRANT}), timeout=2)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert GRANT not in response.text
        assert attempt.verifier not in response.text
        assert "replaceState" in response.text
        assert future.result(timeout=2) == GRANT
    assert auth.key_info(attempt.exchange(GRANT))["limit"] == 5
    with pytest.raises(auth.OpenRouterAuthError, match="already consumed"):
        attempt.exchange(GRANT)
    assert len(api.calls) == 2
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", urlsplit(callback).port), timeout=0.2)


def test_declined_callback_closes_listener_without_exchange(api):
    announced = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            auth.browser_grant,
            auth.LoginAttempt(timeout=5),
            announce=announced.put,
            open_browser=lambda _: None,
        )
        callback = parse_qs(urlsplit(announced.get(timeout=2)).query)["callback_url"][0]
        response = requests.get(callback + "?error=access_denied", timeout=2)
        assert response.status_code == 200
        with pytest.raises(auth.OpenRouterAuthError, match="declined"):
            future.result(timeout=2)
    assert api.calls == []


def test_cancelled_browser_login_releases_socket(api):
    urls = []

    def cancel(_url):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        auth.browser_grant(
            auth.LoginAttempt(), announce=urls.append, open_browser=cancel
        )
    callback = parse_qs(urlsplit(urls[0]).query)["callback_url"][0]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", urlsplit(callback).port), timeout=0.2)
    assert api.calls == []


def test_expired_grant_and_replay_make_no_additional_provider_calls(api):
    attempt = auth.LoginAttempt(timeout=1)
    attempt.created_at -= 2
    with pytest.raises(auth.OpenRouterAuthError, match="expired"):
        attempt.exchange(GRANT)
    assert api.calls == []
    attempt = auth.LoginAttempt()
    api.status = 403
    with pytest.raises(auth.OpenRouterAuthError, match="HTTP 403"):
        attempt.exchange(GRANT)
    with pytest.raises(auth.OpenRouterAuthError, match="already consumed"):
        attempt.exchange(GRANT)
    assert len(api.calls) == 1
    assert attempt.verifier not in repr(attempt)


@pytest.mark.parametrize("status", [301, 401, 403, 429, 503])
def test_provider_error_bodies_are_never_logged_or_retried(api, status, capsys):
    api.status = status
    api.override = {"error": KEY + GRANT}
    with pytest.raises(auth.OpenRouterAuthError) as error:
        auth.LoginAttempt().exchange(GRANT)
    assert KEY not in str(error.value)
    assert GRANT not in str(error.value)
    assert len(api.calls) == 1
    assert KEY not in str(capsys.readouterr())


def test_management_key_cannot_be_saved_by_login(api, vault):
    api.management = True
    with pytest.raises(auth.OpenRouterAuthError, match="management key") as error:
        auth.login(headless=True, read_code=lambda _: GRANT, announce=lambda _: None)
    assert KEY not in str(error.value)
    assert vault.key is None


def test_no_usable_keyring_fails_before_minting_instead_of_saving_plaintext(
    api, monkeypatch
):
    monkeypatch.setattr(auth, "_os_keyring", lambda: None)
    with pytest.raises(auth.OpenRouterAuthError, match="explicitly use --store file"):
        auth.login(headless=True, read_code=lambda _: GRANT, announce=lambda _: None)
    assert api.calls == []
    assert not auth._credential_root().exists()


def test_plaintext_or_unknown_keyring_backend_is_not_an_implicit_fallback(monkeypatch):
    class Plaintext:
        __module__ = "keyrings.alt.file"
        priority = 100

    monkeypatch.setitem(
        sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: Plaintext())
    )
    assert REAL_KEYRING_RESOLVER() is None


def test_os_keyring_errors_do_not_expose_backend_details(vault, monkeypatch):
    def fail(*_args):
        raise RuntimeError(KEY)

    monkeypatch.setattr(vault, "get_password", fail)
    with pytest.raises(auth.OpenRouterAuthError) as error:
        auth.credential()
    assert KEY not in str(error.value)


def test_explicit_file_login_is_private_atomic_and_used_by_retrieval(api):
    from codenib.agent.runtime.grep_jev import GrepJevConfig

    result = auth.login(
        store="file", headless=True, read_code=lambda _: GRANT, announce=lambda _: None
    )
    root = auth._credential_root()
    path = root / "openrouter.json"
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["key"] == KEY
    assert {p.name for p in root.iterdir()} == {path.name, COMPILER_CACHE_LOCK_FILENAME}
    assert result["store"] == "file"
    assert auth.credential() == (KEY, "file")
    assert GrepJevConfig().credential() == KEY
    auth.forget_key(store="file")
    assert auth.credential() == (None, "none")


def test_environment_override_is_explicit_and_never_persisted(api, vault, monkeypatch):
    vault.key = KEY
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-secret")
    assert auth.credential() == ("environment-secret", "environment")
    auth.forget_key(store="keyring")
    assert auth.credential() == ("environment-secret", "environment")
    assert not auth._credential_root().exists()


def test_failed_atomic_replacement_preserves_old_key_and_cleans_temporary_file(
    monkeypatch,
):
    auth.save_key(KEY, store="file")

    def fail(*_args, **_kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(auth.os, "replace", fail)
    with pytest.raises(auth.OpenRouterAuthError):
        auth.save_key("new-key-must-not-be-partially-visible", store="file")
    assert auth.credential() == (KEY, "file")
    assert {path.name for path in auth._credential_root().iterdir()} == {
        "openrouter.json",
        COMPILER_CACHE_LOCK_FILENAME,
    }


@pytest.mark.parametrize("payload", [b"{", b'{"schema":2,"key":"broken"}', b"x" * 9000])
def test_logout_can_remove_corrupt_credential_payload_without_parsing_it(payload):
    auth.save_key(KEY, store="file")
    path = auth._credential_root() / "openrouter.json"
    path.write_bytes(payload)
    with pytest.raises(auth.OpenRouterAuthError):
        auth.credential()
    auth.forget_key(store="file")
    assert not path.exists()
    auth.save_key(KEY, store="file")
    assert auth.credential() == (KEY, "file")


def test_default_logout_succeeds_for_file_fallback_with_no_keyring(monkeypatch, capsys):
    from codenib.cli import run

    auth.save_key(KEY, store="file")
    monkeypatch.setattr(auth, "_os_keyring", lambda: None)
    assert run(["auth", "logout"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["local_removal_complete"]
    assert report["unavailable_stores"] == ["keyring"]
    assert not (auth._credential_root() / "openrouter.json").exists()
    assert run(["auth", "logout", "--store", "keyring"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["local_removal_complete"]


def _crash_before_credential_publication(root):
    auth._credential_root = lambda: Path(root)

    def die(*_args, **_kwargs):
        os._exit(0)

    auth.os.replace = die
    auth.save_key(KEY, store="file")


@pytest.mark.skipif(os.name != "posix", reason="explicit POSIX file fallback")
def test_logout_recovers_secret_temporary_left_by_a_killed_writer():
    root = auth._credential_root()
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_before_credential_publication, args=(str(root),)
    )
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    temporaries = list(root.glob(".openrouter-*"))
    assert len(temporaries) == 1
    assert KEY.encode() in temporaries[0].read_bytes()
    assert not (root / "openrouter.json").exists()
    auth.forget_key(store="file")
    assert not list(root.glob(".openrouter-*"))
    assert all(KEY.encode() not in p.read_bytes() for p in root.iterdir())


def test_logout_refuses_symlinked_recovery_entry_without_touching_target(tmp_path):
    auth.prepare_store("file")
    outside = tmp_path / "outside"
    outside.write_text("must remain")
    orphan = auth._credential_root() / (".openrouter-" + "a" * 32)
    orphan.symlink_to(outside)
    with pytest.raises(auth.OpenRouterAuthError, match="unsafe credential file"):
        auth.forget_key(store="file")
    assert orphan.is_symlink()
    assert outside.read_text() == "must remain"


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "hardlink", "fifo"])
def test_unsafe_credential_file_cannot_be_read(api, tmp_path, unsafe):
    root = auth._credential_root()
    root.mkdir(parents=True, mode=0o700)
    path = root / "openrouter.json"
    if unsafe == "symlink":
        outside = tmp_path / "outside"
        outside.write_text(json.dumps({"schema": 1, "key": KEY}))
        outside.chmod(0o600)
        path.symlink_to(outside)
    elif unsafe == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        path.write_text(json.dumps({"schema": 1, "key": KEY}))
        path.chmod(0o644 if unsafe == "permissions" else 0o600)
        if unsafe == "hardlink":
            os.link(path, tmp_path / "other-link")
    with pytest.raises(auth.OpenRouterAuthError):
        auth.credential()
    assert api.calls == []


def test_symlinked_storage_parent_and_git_checkout_are_rejected(
    api, tmp_path, monkeypatch
):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(auth, "_credential_root", lambda: link / "credentials")
    with pytest.raises(auth.OpenRouterAuthError):
        auth.prepare_store("file")
    assert not (real / "credentials").exists()
    (real / ".git").mkdir()
    monkeypatch.setattr(auth, "_credential_root", lambda: real / "credentials")
    with pytest.raises(auth.OpenRouterAuthError, match="outside Git"):
        auth.prepare_store("file")
    assert not (real / "credentials").exists()
    assert api.calls == []


def test_cli_import_status_and_logout_redact_key(api, vault, monkeypatch, capsys):
    from codenib.cli import run

    monkeypatch.setattr("getpass.getpass", lambda _: KEY)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run(["auth", "login", "--import-key"]) == 0
    assert vault.key == KEY
    assert KEY not in capsys.readouterr().out
    assert run(["auth", "status", "--remote"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["connected"] is True and result["source"] == "keyring"
    assert result["limit_remaining"] == 4
    assert KEY not in json.dumps(result)
    assert run(["auth", "logout"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["local_removal_complete"] is True
    assert result["provider_revoked"] is False
    assert vault.key is None
