# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Publication guards shared by static sites and context artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .._atomic_directory import PublicationDirectoryReader
from .._bounded_json import (
    DEFAULT_MAX_ATOM_BYTES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_KEY_BYTES,
    DEFAULT_MAX_LEXICAL_TOKENS,
    DEFAULT_MAX_NODES_PER_ELEMENT,
    DEFAULT_MAX_STRING_BYTES,
    validate_bounded_json_stream,
    validate_json_complexity,
)
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
_MAX_CANONICAL_SCALAR_BYTES = 1_024
_MAX_PUBLISHABLE_JSON_BYTES = 64 * 1024 * 1024
_MAX_PUBLISHABLE_JSON_NODES = DEFAULT_MAX_NODES_PER_ELEMENT
_MAX_PUBLISHABLE_JSON_TOKENS = DEFAULT_MAX_LEXICAL_TOKENS
_MAX_PUBLISHABLE_JSON_DEPTH = DEFAULT_MAX_DEPTH
_MAX_PUBLISHABLE_JSON_KEY_BYTES = DEFAULT_MAX_KEY_BYTES
_MAX_PUBLISHABLE_JSON_STRING_BYTES = DEFAULT_MAX_STRING_BYTES
_MAX_PUBLISHABLE_JSON_ATOM_BYTES = DEFAULT_MAX_ATOM_BYTES


def assert_no_credential_fields(value: Any, *, source: str) -> None:
    """Reject credential-shaped keys in metadata intended for publication."""

    assert_no_secret_fields(value, source=source)


def _forbidden_path_strings(paths: Iterable[Path]) -> tuple[str, ...]:
    values: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        try:
            raw = os.fsdecode(os.fspath(expanded))
        except TypeError:
            raw = str(expanded)
        lexical = os.path.abspath(raw)
        if os.path.isabs(raw):
            values.add(raw)
        values.update((lexical, Path(lexical).as_posix()))
        try:
            canonical = expanded.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        values.add(str(canonical))
        as_posix = getattr(canonical, "as_posix", None)
        if callable(as_posix):
            values.add(as_posix())
    return tuple(sorted(value for value in values if value))


def assert_publishable_json_value(
    value: Any,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
) -> None:
    """Scan decoded JSON semantics without depending on their source encoding."""

    assert_no_credential_fields(value, source=label)
    forbidden = _forbidden_path_strings(forbidden_paths)
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

    def check_text(text: str) -> None:
        if any(pattern in text for pattern in forbidden):
            raise ValueError(f"{label} contains an absolute build-machine path")
        if any(pattern in text for pattern in secrets):
            raise ValueError(f"{label} contains a configured credential")

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} contains a non-text JSON object key")
                check_text(key)
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            check_text(current)
        elif current is None or isinstance(current, bool):
            check_text("null" if current is None else ("true" if current else "false"))
        elif isinstance(current, int):
            # Avoid materializing an attacker-sized integer representation.
            if current.bit_length() > 3_404:
                raise ValueError(f"{label} contains an oversized JSON integer")
            scalar = str(current)
            if len(scalar) > _MAX_CANONICAL_SCALAR_BYTES:
                raise ValueError(f"{label} contains an oversized JSON integer")
            check_text(scalar)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label} contains a non-finite JSON number")
            scalar = json.dumps(current, allow_nan=False, separators=(",", ":"))
            check_text(scalar)
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


def _matching_kind_blocks(
    blocks: Iterable[bytes],
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
    for chunk in blocks:
        window = tail + chunk
        for kind, needle in needles:
            if needle in window:
                return kind
        tail = window[-overlap:] if overlap else b""
    return None


def _reject_duplicate_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate publication JSON key: {key}")
        result[key] = value
    return result


def _bounded_publication_int(value: str) -> int:
    if len(value) > _MAX_CANONICAL_SCALAR_BYTES:
        raise ValueError("publication JSON integer is too large")
    return int(value)


def _bounded_publication_float(value: str) -> float:
    if len(value) > _MAX_CANONICAL_SCALAR_BYTES:
        raise ValueError("publication JSON number is too large")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("publication JSON number is not finite")
    return parsed


def _reject_publication_constant(value: str) -> None:
    raise ValueError(f"publication JSON constant is not finite: {value}")


def assert_publishable_tree_reader(
    reader: PublicationDirectoryReader,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    max_json_bytes: int = _MAX_PUBLISHABLE_JSON_BYTES,
) -> None:
    """Apply publication policy through one authenticated tree authority.

    Publication JSON is UTF-8 without a byte-order mark. Its lexical resource
    budgets are enforced before strict UTF-8 decoding and DOM allocation.
    """

    if (
        isinstance(max_json_bytes, bool)
        or not isinstance(max_json_bytes, int)
        or max_json_bytes <= 0
        or max_json_bytes > _MAX_PUBLISHABLE_JSON_BYTES
    ):
        raise ValueError("publishable JSON byte limit is out of bounds")
    roots = tuple(forbidden_paths)
    forbidden = _serialized_patterns(_forbidden_path_strings(roots))
    secrets = _secret_values(environ)

    def is_json_path(path: str) -> bool:
        return path.casefold().endswith(".json")

    def entry_policy(path: str, kind: str, mode: int, size: int) -> None:
        del mode
        encoded = path.encode("utf-8", errors="strict")
        if any(secret in encoded for secret in secrets):
            raise ValueError(f"{label} contains a configured credential in {path}")
        if kind == "file" and is_json_path(path) and size > max_json_bytes:
            raise ValueError(
                f"{label} JSON file exceeds its {max_json_bytes}-byte limit: {path}"
            )

    reader.capture_ownership(entry_policy=entry_policy)
    for record in reader.file_records():
        relative = record.path
        if is_json_path(relative):
            json_label = f"{label} JSON {relative}"
            with reader.open_authenticated_file(
                relative,
                max_bytes=max_json_bytes,
            ) as source:
                validate_bounded_json_stream(
                    source,
                    label=json_label,
                    max_bytes=max_json_bytes,
                    max_nodes=_MAX_PUBLISHABLE_JSON_NODES,
                    max_lexical_tokens=_MAX_PUBLISHABLE_JSON_TOKENS,
                    max_depth=_MAX_PUBLISHABLE_JSON_DEPTH,
                    max_key_bytes=_MAX_PUBLISHABLE_JSON_KEY_BYTES,
                    max_string_bytes=_MAX_PUBLISHABLE_JSON_STRING_BYTES,
                    max_atom_bytes=_MAX_PUBLISHABLE_JSON_ATOM_BYTES,
                )
            payload = reader.read_bytes(relative, max_bytes=max_json_bytes)
            match = _matching_kind_blocks(
                (payload,),
                forbidden=forbidden,
                secrets=secrets,
            )
            try:
                serialized = payload.decode("utf-8", errors="strict")
                decoded = json.loads(
                    serialized,
                    object_pairs_hook=_reject_duplicate_json_object,
                    parse_int=_bounded_publication_int,
                    parse_float=_bounded_publication_float,
                    parse_constant=_reject_publication_constant,
                )
            except (RecursionError, ValueError) as exc:
                raise ValueError(
                    f"{label} contains invalid JSON "
                    f"(UTF-8 without BOM required) in {relative}"
                ) from exc
            validate_json_complexity(
                decoded,
                label=json_label,
                max_nodes=_MAX_PUBLISHABLE_JSON_NODES,
                max_depth=_MAX_PUBLISHABLE_JSON_DEPTH,
                max_key_bytes=_MAX_PUBLISHABLE_JSON_KEY_BYTES,
            )
            assert_publishable_json_value(
                decoded,
                forbidden_paths=roots,
                environ=environ,
                label=json_label,
            )
        else:
            with reader.open_authenticated_file(
                relative,
                max_bytes=record.size,
            ) as source:
                match = _matching_kind_blocks(
                    source.iter_bytes(chunk_size=_SCAN_CHUNK_BYTES),
                    forbidden=forbidden,
                    secrets=secrets,
                )
        if match == "path":
            raise ValueError(
                f"{label} contains an absolute build-machine path in {relative}"
            )
        if match == "secret":
            raise ValueError(f"{label} contains a configured credential in {relative}")
    reader.capture_ownership(entry_policy=entry_policy)


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
    "assert_publishable_tree_reader",
    "file_sha256",
]
