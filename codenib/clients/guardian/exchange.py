# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Commit-addressed filesystem exchange for external Guardian controllers.

The coding-agent environment is an untrusted publisher.  It writes a Git
bundle and a small manifest into a controller-owned exchange directory.  The
controller validates both before materializing a fresh checkout for review.
No path supplied by the publisher is interpreted as an arbitrary host path.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{40}$")
SCHEMA_VERSION = 1
DEFAULT_MAX_BUNDLE_BYTES = 256 * 1024**2


class ExchangeProtocolError(ValueError):
    """An exchange artifact is malformed, stale, or fails validation."""


def is_request_id(value: object) -> bool:
    """Return whether ``value`` is a canonical commit-addressed request ID."""

    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class ReviewExchangeRequest:
    """A request to review one exact transition between Git commits."""

    request_id: str
    base_commit: str
    candidate_commit: str
    bundle_name: str
    bundle_sha256: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ExchangeProtocolError(
                f"unsupported exchange schema version: {self.schema_version}"
            )
        for name in ("request_id", "base_commit", "candidate_commit"):
            value = getattr(self, name)
            valid = (
                is_request_id(value)
                if name == "request_id"
                else (isinstance(value, str) and _REVISION.fullmatch(value) is not None)
            )
            if not valid:
                raise ExchangeProtocolError(f"{name} must be a full lowercase Git SHA")
        if self.request_id != self.candidate_commit:
            raise ExchangeProtocolError("request_id must equal candidate_commit")
        expected_bundle = f"{self.request_id}.bundle"
        if self.bundle_name != expected_bundle:
            raise ExchangeProtocolError(
                f"bundle_name must be the canonical name {expected_bundle!r}"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.bundle_sha256):
            raise ExchangeProtocolError("bundle_sha256 must be a lowercase SHA-256")
        if self.base_commit == self.candidate_commit:
            raise ExchangeProtocolError("base and candidate commits must differ")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> "ReviewExchangeRequest":
        if not isinstance(raw, dict):
            raise ExchangeProtocolError("request manifest must be a JSON object")
        try:
            return cls(
                schema_version=int(raw["schema_version"]),
                request_id=str(raw["request_id"]),
                base_commit=str(raw["base_commit"]),
                candidate_commit=str(raw["candidate_commit"]),
                bundle_name=str(raw["bundle_name"]),
                bundle_sha256=str(raw["bundle_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ExchangeProtocolError):
                raise
            raise ExchangeProtocolError(f"invalid request manifest: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_exchange_request(
    exchange_root: Path,
    manifest_path: Path,
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> tuple[ReviewExchangeRequest, Path]:
    """Load a manifest and resolve its bundle below ``exchange_root``."""

    root = exchange_root.resolve(strict=True)
    manifest = manifest_path.resolve(strict=True)
    requests_dir = (root / "requests").resolve(strict=True)
    if manifest.parent != requests_dir or manifest.suffix != ".json":
        raise ExchangeProtocolError("manifest is outside the canonical requests dir")
    try:
        request = ReviewExchangeRequest.from_dict(
            json.loads(manifest.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ExchangeProtocolError(f"cannot read request manifest: {exc}") from exc
    if manifest.name != f"{request.request_id}.json":
        raise ExchangeProtocolError("manifest filename does not match request_id")

    bundle = root / "bundles" / request.bundle_name
    try:
        bundle = bundle.resolve(strict=True)
    except OSError as exc:
        raise ExchangeProtocolError(f"bundle is unavailable: {exc}") from exc
    bundles_dir = (root / "bundles").resolve(strict=True)
    if bundle.parent != bundles_dir or not bundle.is_file():
        raise ExchangeProtocolError("bundle is outside the canonical bundles dir")
    size = bundle.stat().st_size
    if size <= 0 or size > max_bundle_bytes:
        raise ExchangeProtocolError(f"bundle size {size} is outside the accepted range")
    if _sha256(bundle) != request.bundle_sha256:
        raise ExchangeProtocolError("bundle checksum does not match manifest")
    return request, bundle


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExchangeProtocolError(f"Git bundle validation failed: {exc}") from exc
    return result.stdout.strip()


def materialize_exchange_request(
    request: ReviewExchangeRequest,
    bundle: Path,
    destination: Path,
) -> Path:
    """Validate the advertised commits and create a detached candidate checkout."""

    destination.mkdir(parents=True, exist_ok=False)
    _git("init", "--quiet", cwd=destination)
    _git(
        "fetch",
        "--quiet",
        "--no-tags",
        str(bundle),
        "refs/guardian/base:refs/guardian/base",
        "refs/guardian/candidate:refs/guardian/candidate",
        cwd=destination,
    )
    base = _git("rev-parse", "refs/guardian/base^{commit}", cwd=destination)
    candidate = _git("rev-parse", "refs/guardian/candidate^{commit}", cwd=destination)
    if base != request.base_commit or candidate != request.candidate_commit:
        raise ExchangeProtocolError("bundle refs do not match the advertised commits")
    _git("merge-base", "--is-ancestor", base, candidate, cwd=destination)
    _git("checkout", "--quiet", "--detach", candidate, cwd=destination)
    return destination


__all__ = [
    "DEFAULT_MAX_BUNDLE_BYTES",
    "ExchangeProtocolError",
    "ReviewExchangeRequest",
    "SCHEMA_VERSION",
    "is_request_id",
    "load_exchange_request",
    "materialize_exchange_request",
]
