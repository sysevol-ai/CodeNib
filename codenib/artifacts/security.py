# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Publication guards shared by static sites and context artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .._secret_fields import assert_no_secret_fields

_SENSITIVE_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_API_KEY",
    "CODENIB_DEMO_API_KEY",
    "CODENIB_ACTION_EMBEDDING_KEY",
    "CODENIB_EMBEDDING_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
}
_SENSITIVE_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
_SCAN_CHUNK_BYTES = 1024 * 1024


def assert_no_credential_fields(value: Any, *, source: str) -> None:
    """Reject credential-shaped keys in metadata intended for publication."""

    assert_no_secret_fields(value, source=source)


def assert_publishable_json_value(
    value: Any,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
) -> None:
    """Scan decoded JSON semantics without depending on their source encoding."""

    assert_no_credential_fields(value, source=label)
    forbidden_values: list[str] = []
    for path in forbidden_paths:
        resolved = path.expanduser().resolve()
        forbidden_values.extend((str(resolved), resolved.as_posix()))
    forbidden = tuple(item for item in forbidden_values if item)
    secrets = tuple(
        value
        for name, value in environ.items()
        if isinstance(value, str)
        and len(value) >= 8
        and (
            name.upper() in _SENSITIVE_ENV_NAMES
            or name.upper().endswith(_SENSITIVE_ENV_SUFFIXES)
        )
    )
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} contains a non-text JSON object key")
                if any(pattern in key for pattern in forbidden):
                    raise ValueError(f"{label} contains an absolute build-machine path")
                if any(pattern in key for pattern in secrets):
                    raise ValueError(f"{label} contains a configured credential")
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            if any(pattern in current for pattern in forbidden):
                raise ValueError(f"{label} contains an absolute build-machine path")
            if any(pattern in current for pattern in secrets):
                raise ValueError(f"{label} contains a configured credential")
        elif current is None or isinstance(current, (bool, int)):
            continue
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label} contains a non-finite JSON number")
        else:
            raise ValueError(
                f"{label} contains unsupported JSON value: " f"{type(current).__name__}"
            )


def _serialized_patterns(values: Iterable[str]) -> tuple[bytes, ...]:
    patterns: set[bytes] = set()
    for value in values:
        if not value:
            continue
        patterns.add(value.encode("utf-8"))
        escaped = json.dumps(value, ensure_ascii=True)[1:-1]
        patterns.add(escaped.encode("utf-8"))
    return tuple(sorted(patterns, key=lambda item: (-len(item), item)))


def _secret_values(environ: Mapping[str, str]) -> tuple[bytes, ...]:
    values: set[str] = set()
    for name, value in environ.items():
        upper = name.upper()
        sensitive = upper in _SENSITIVE_ENV_NAMES or upper.endswith(
            _SENSITIVE_ENV_SUFFIXES
        )
        if sensitive and len(value) >= 8:
            values.add(value)
    return _serialized_patterns(values)


def file_sha256(path: Path) -> tuple[int, str]:
    """Return byte length and SHA-256 without loading a full artifact file."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_SCAN_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _matching_kind(
    path: Path,
    *,
    forbidden: tuple[bytes, ...],
    secrets: tuple[bytes, ...],
) -> str | None:
    needles = tuple(
        (kind, needle)
        for kind, values in (("path", forbidden), ("secret", secrets))
        for needle in values
        if needle
    )
    if not needles:
        return None
    overlap = max(len(needle) for _kind, needle in needles) - 1
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(_SCAN_CHUNK_BYTES):
            window = tail + chunk
            for kind, needle in needles:
                if needle in window:
                    return kind
            tail = window[-overlap:] if overlap else b""
    return None


def _assert_publishable_file(
    path: Path,
    *,
    root: Path,
    forbidden: tuple[bytes, ...],
    secrets: tuple[bytes, ...],
    label: str,
) -> None:
    relative = path.relative_to(root)
    if path.is_symlink():
        raise ValueError(f"{label} contains a symbolic link: {relative}")
    if any(secret in relative.as_posix().encode("utf-8") for secret in secrets):
        raise ValueError(f"{label} contains a configured credential in {relative}")
    if not path.is_file():
        return
    match = _matching_kind(path, forbidden=forbidden, secrets=secrets)
    if match == "path":
        raise ValueError(
            f"{label} contains an absolute build-machine path in {relative}"
        )
    if match == "secret":
        raise ValueError(f"{label} contains a configured credential in {relative}")


def assert_publishable_tree(
    root: Path,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
) -> None:
    """Reject links, build-machine paths, and configured secrets in a tree."""

    resolved_root = root.expanduser().resolve()
    forbidden_values: list[str] = []
    for path in forbidden_paths:
        resolved = path.expanduser().resolve()
        forbidden_values.extend((str(resolved), resolved.as_posix()))
    forbidden = _serialized_patterns(forbidden_values)
    secrets = _secret_values(environ)
    for path in sorted(resolved_root.rglob("*")):
        _assert_publishable_file(
            path,
            root=resolved_root,
            forbidden=forbidden,
            secrets=secrets,
            label=label,
        )


__all__ = [
    "assert_no_credential_fields",
    "assert_publishable_json_value",
    "assert_publishable_tree",
    "file_sha256",
]
