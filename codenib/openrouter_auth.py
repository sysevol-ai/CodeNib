# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Local OpenRouter PKCE and credentials; no CodeNib service receives a key.

One login owns the verifier and callback listener until it completes, expires,
or is cancelled. A grant is consumed locally before its single exchange call.
The file fallback is explicit, POSIX-only, and unencrypted. Its private directory
is held by an fd; replacement is the save linearization point. No persistent
login worker, refresh token, management key, or credential registry is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import stat
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

from .paths import user_state_dir

_SERVICE = "codenib/openrouter"
_ACCOUNT = "default"
_CREDENTIAL_FILE = "openrouter.json"
_OS_KEYRINGS = {
    "keyring.backends.SecretService",
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.libsecret",
    "keyring.backends.kwallet",
}


class OpenRouterAuthError(RuntimeError):
    """Safe error text: never include a credential, grant, or provider body."""


def validate_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenRouterAuthError("An OpenRouter API key is required")
    key = value.strip()
    if len(key) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise OpenRouterAuthError("The OpenRouter API key has invalid characters")
    return key


def _os_keyring():
    try:
        import keyring

        backend = keyring.get_keyring()
        choices = (
            backend.backends
            if type(backend).__module__ == "keyring.backends.chainer"
            else [backend]
        )
        return next(
            (
                item
                for item in choices
                if type(item).__module__ in _OS_KEYRINGS and item.priority >= 1
            ),
            None,
        )
    except Exception:
        return None


def _credential_root() -> Path:
    return Path(os.path.abspath(user_state_dir())) / "credentials"


@contextmanager
def _credential_directory(*, create: bool = False) -> Iterator[int]:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise OpenRouterAuthError("File credentials require POSIX; use the OS keyring")
    root = _credential_root()
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise OpenRouterAuthError("Credential files must be outside Git repositories")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            try:
                following = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                following = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OpenRouterAuthError(
                "Credential directory must be user-owned with mode 0700"
            )
        yield fd
    finally:
        os.close(fd)


def _check_file(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 8192
    ):
        raise OpenRouterAuthError(
            "Credential file must be user-owned, regular, private (0600), and bounded"
        )


def _read_file_key() -> str | None:
    # Windows uses the OS credential manager; do not pretend mode bits are ACLs.
    if os.name != "posix":
        return None
    try:
        # Absence must not stop an OS-keyring login when CODENIB_HOME points
        # into a checkout. A present file still passes the full fd-based checks.
        (_credential_root() / _CREDENTIAL_FILE).lstat()
        with _credential_directory() as directory:
            fd = os.open(
                _CREDENTIAL_FILE,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            try:
                _check_file(fd)
                data = json.loads(os.read(fd, 8193))
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise OpenRouterAuthError(
            "Cannot read the private OpenRouter credential file"
        ) from None
    if (
        not isinstance(data, dict)
        or type(data.get("schema")) is not int
        or data.get("schema") != 1
        or set(data) != {"schema", "key"}
    ):
        raise OpenRouterAuthError(
            "The OpenRouter credential file has an invalid format"
        )
    return validate_key(data["key"])


def credential() -> tuple[str | None, str]:
    """Environment first; an explicitly created file otherwise precedes keyring."""
    supplied = os.environ.get("OPENROUTER_API_KEY")
    if supplied is not None:
        return validate_key(supplied), "environment"
    saved = _read_file_key()
    if saved is not None:
        return saved, "file"
    backend = _os_keyring()
    if backend is not None:
        try:
            saved = backend.get_password(_SERVICE, _ACCOUNT)
        except Exception:
            raise OpenRouterAuthError(
                "Cannot unlock the OS OpenRouter credential; "
                "use your keyring or OPENROUTER_API_KEY"
            ) from None
        if saved is not None:
            return validate_key(saved), "keyring"
    return None, "none"


def require_key() -> str:
    key, _ = credential()
    if key is None:
        raise OpenRouterAuthError("Run `codenib auth login` or set OPENROUTER_API_KEY")
    return key


def prepare_store(store: str) -> None:
    """Check storage before asking the user to mint a new key."""
    if store == "file":
        try:
            with _credential_directory(create=True):
                pass
            _read_file_key()
        except OSError:
            raise OpenRouterAuthError(
                "Cannot open a private credential directory without symlinks"
            ) from None
        return
    if store != "keyring":
        raise ValueError("credential store must be keyring or file")
    if _read_file_key() is not None:
        raise OpenRouterAuthError(
            "Remove the saved file with `codenib auth logout --store file` "
            "before switching to keyring"
        )
    backend = _os_keyring()
    if backend is None:
        raise OpenRouterAuthError(
            "OS keyring unavailable; install codenib[auth], or explicitly use "
            "--store file for unencrypted local storage"
        )
    try:
        backend.get_password(_SERVICE, _ACCOUNT)
    except Exception:
        raise OpenRouterAuthError(
            "Cannot unlock the OS keyring; unlock it or explicitly choose --store file"
        ) from None


def save_key(key: str, *, store: str) -> None:
    key = validate_key(key)
    prepare_store(store)
    if store == "keyring":
        try:
            _os_keyring().set_password(_SERVICE, _ACCOUNT, key)
        except Exception:
            raise OpenRouterAuthError(
                "Could not save the OpenRouter key in the OS keyring; "
                "revoke the new key on OpenRouter if needed"
            ) from None
        return
    temporary = f".openrouter-{secrets.token_hex(16)}"
    try:
        with _credential_directory() as directory:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(json.dumps({"schema": 1, "key": key}).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(
                    temporary,
                    _CREDENTIAL_FILE,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except OSError:
        raise OpenRouterAuthError(
            "Could not save the private OpenRouter credential file; "
            "revoke the new key on OpenRouter if needed"
        ) from None


def forget_key(*, store: str) -> None:
    """Forget one local copy. Provider revocation remains an explicit user action."""
    if store == "file":
        if _read_file_key() is None:
            return
        with _credential_directory() as directory:
            os.unlink(_CREDENTIAL_FILE, dir_fd=directory)
            os.fsync(directory)
    elif store == "keyring":
        backend = _os_keyring()
        if backend is None:
            raise OpenRouterAuthError("OS keyring unavailable; no key was removed")
        try:
            if backend.get_password(_SERVICE, _ACCOUNT) is not None:
                backend.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            raise OpenRouterAuthError(
                "Could not remove the OS OpenRouter credential"
            ) from None
    else:
        raise ValueError("credential store must be keyring or file")


def _request_json(
    method: str, path: str, *, key: str | None = None, payload=None
) -> dict:
    import requests

    headers = {"Content-Type": "application/json", "X-OpenRouter-Title": "CodeNib"}
    if key is not None:
        headers["Authorization"] = f"Bearer {validate_key(key)}"
    try:
        with requests.request(
            method,
            f"https://openrouter.ai/api/v1/{path}",
            headers=headers,
            json=payload,
            timeout=(5, 20),
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise OpenRouterAuthError(
                    f"OpenRouter authorization failed (HTTP {response.status_code}); no retry"
                )
            chunks, size = [], 0
            for chunk in response.iter_content(4096):
                size += len(chunk)
                if size > 16384:
                    raise OpenRouterAuthError(
                        "OpenRouter authorization response exceeded its size limit"
                    )
                chunks.append(chunk)
            body = json.loads(b"".join(chunks))
    except requests.RequestException:
        raise OpenRouterAuthError(
            "OpenRouter authorization transport failed; no retry"
        ) from None
    except ValueError:
        raise OpenRouterAuthError(
            "OpenRouter authorization returned invalid JSON"
        ) from None
    if not isinstance(body, dict):
        raise OpenRouterAuthError(
            "OpenRouter authorization returned an invalid response"
        )
    return body


def key_info(key: str) -> dict:
    """Return only useful nonsecret account limits; refuse management credentials."""
    data = _request_json("GET", "key", key=key).get("data")
    if not isinstance(data, dict):
        raise OpenRouterAuthError("OpenRouter did not return API key metadata")
    if (
        data.get("is_management_key") is not False
        or data.get("is_provisioning_key", False) is not False
    ):
        raise OpenRouterAuthError("Use a normal limited API key, not a management key")
    result = {}
    for field_name in ("limit", "limit_remaining", "usage"):
        value = data.get(field_name)
        if value is None:
            result[field_name] = None
        elif (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            result[field_name] = value
        else:
            raise OpenRouterAuthError("OpenRouter returned invalid API key limits")
    # Do not echo labels, arbitrary strings or provider error bodies.
    return result


def settings_url(key: str) -> str:
    fingerprint = hashlib.sha256(validate_key(key).encode()).hexdigest()
    return f"https://openrouter.ai/keys/{fingerprint}"


@dataclass
class LoginAttempt:
    timeout: float = 300.0
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(64), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    created_at: float = field(default_factory=time.monotonic, repr=False)
    used: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 600:
            raise ValueError("authorization timeout must be between 1 and 600 seconds")

    def check(self) -> None:
        if self.used or time.monotonic() - self.created_at >= self.timeout:
            raise OpenRouterAuthError(
                "OpenRouter authorization expired or was already consumed; start a new login"
            )

    def authorization_url(self, callback: str | None = None) -> str:
        self.check()
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "key_label": "CodeNib",
        }
        if callback:
            params["callback_url"] = callback
        return "https://openrouter.ai/auth?" + urlencode(params)

    def exchange(self, code: str) -> str:
        self.check()
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in code)
        ):
            raise OpenRouterAuthError("Invalid OpenRouter authorization code")
        self.used = True
        body = _request_json(
            "POST",
            "auth/keys",
            payload={
                "code": code,
                "code_verifier": self.verifier,
                "code_challenge_method": "S256",
            },
        )
        return validate_key(body.get("key"))


def browser_grant(
    attempt: LoginAttempt,
    *,
    announce: Callable[[str], None] = print,
    open_browser: Callable[[str], object] = webbrowser.open,
) -> str:
    """Accept exactly one callback at a random path on a loopback-only listener."""
    callback_path = f"/openrouter/callback/{attempt.nonce}"
    accepted: list[str] = []
    declined = False
    script_nonce = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def log_message(self, *_args):
            pass  # Callback URLs contain grants and must not be logged.

        def do_GET(self):
            nonlocal declined
            parsed = urlsplit(self.path)
            if (
                self.headers.get("Host") != f"localhost:{self.server.server_port}"
                or parsed.scheme
                or parsed.netloc
                or parsed.path != callback_path
                or len(self.path) > 4096
                or accepted
                or declined
            ):
                self.send_error(404, "Not found")
                return
            try:
                attempt.check()
                params = parse_qs(parsed.query, strict_parsing=True, max_num_fields=8)
            except (ValueError, OpenRouterAuthError):
                self.send_error(400, "Invalid or expired authorization")
                return
            if "error" in params:
                declined = True
                message = "Authorization declined. Return to the terminal."
            elif len(params.get("code", [])) == 1 and len(params["code"][0]) <= 2048:
                accepted.append(params["code"][0])
                message = "Authorization received. Return to the terminal."
            else:
                self.send_error(400, "Invalid authorization")
                return
            body = (
                "<!doctype html><title>CodeNib authorization</title>"
                f"<script nonce={script_nonce!r}>"
                "history.replaceState(null,'','/connected')</script>"
                f"<p>{message}</p>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; script-src 'nonce-{script_nonce}'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 0.25
        url = attempt.authorization_url(
            f"http://localhost:{server.server_port}{callback_path}"
        )
        announce(url)
        open_browser(url)
        while not accepted and not declined:
            attempt.check()
            server.handle_request()
        if declined:
            raise OpenRouterAuthError("OpenRouter authorization was declined")
    return accepted[0]


def login(
    *,
    store: str = "keyring",
    headless: bool = False,
    timeout: float = 300,
    read_code: Callable[[str], str],
    announce: Callable[[str], None] = print,
) -> dict:
    prepare_store(store)
    attempt = LoginAttempt(timeout=timeout)
    if headless:
        announce(attempt.authorization_url())
        code = read_code("OpenRouter authorization code: ")
    else:
        code = browser_grant(attempt, announce=announce)
    key = attempt.exchange(code.strip())
    try:
        info = key_info(key)
        save_key(key, store=store)
    except OpenRouterAuthError as exc:
        raise OpenRouterAuthError(
            f"{exc}. A new key may exist; review or revoke it at {settings_url(key)}"
        ) from None
    return {
        "connected": True,
        "store": store,
        **info,
        "settings_url": settings_url(key),
    }
