# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Publication guards shared by static sites and context artifacts."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from .._atomic_directory import (
    PublicationDirectoryReader,
    directory_ownership_file_records,
)
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
from .._contained_source import _SourceCleanupGroup
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
_SEMANTIC_SCAN_CHARS = 64 * 1024
_MAX_CANONICAL_SCALAR_BYTES = 1_024
_MAX_PUBLISHABLE_JSON_BYTES = 64 * 1024 * 1024
_MAX_STREAMING_PUBLISHABLE_JSON_BYTES = 256 * 1024 * 1024
_MAX_PUBLISHABLE_JSON_NODES = DEFAULT_MAX_NODES_PER_ELEMENT
_MAX_PUBLISHABLE_JSON_TOKENS = DEFAULT_MAX_LEXICAL_TOKENS
_MAX_PUBLISHABLE_JSON_DEPTH = DEFAULT_MAX_DEPTH
_MAX_PUBLISHABLE_JSON_KEY_BYTES = DEFAULT_MAX_KEY_BYTES
_MAX_PUBLISHABLE_JSON_STRING_BYTES = DEFAULT_MAX_STRING_BYTES
_MAX_PUBLISHABLE_JSON_ATOM_BYTES = DEFAULT_MAX_ATOM_BYTES
_SECURITY_SORT_RUN_ENTRIES = 256
_SecurityItem = TypeVar("_SecurityItem")
_SerializedPattern = bytes | bytearray
_TRUSTED_SIZED_ITERABLE_TYPES = {
    bytes,
    dict,
    frozenset,
    list,
    range,
    set,
    str,
    tuple,
    type({}.items()),
    type({}.keys()),
    type({}.values()),
}


class _CallbackIterationStop(BaseException):
    """Carry iteration sentinels through callback-bearing generators."""

    def __init__(self, error: StopIteration | StopAsyncIteration) -> None:
        super().__init__(error)
        self.error = error


def _transfer_callback_exception_settlement(
    source: BaseException,
    target: BaseException,
) -> None:
    """Move authenticated cleanup observability onto the exact callback stop."""

    try:
        notes = BaseException.__getattribute__(source, "__notes__")
    except AttributeError:
        notes = ()
    if type(notes) is list:
        add_note = getattr(BaseException, "add_note", None)
        if add_note is not None:
            for note in notes:
                if type(note) is str:
                    try:
                        add_note(target, note)
                    except BaseException:  # noqa: B036 - diagnostics only
                        break
    for attribute in ("_codenib_cleanup_notes", "publication_cleanup_owners"):
        try:
            values = BaseException.__getattribute__(source, attribute)
        except AttributeError:
            values = ()
        if type(values) is not tuple:
            continue
        try:
            existing = BaseException.__getattribute__(target, attribute)
        except AttributeError:
            existing = ()
        if type(existing) is not tuple:
            existing = ()
        try:
            BaseException.__setattr__(target, attribute, (*existing, *values))
        except BaseException:  # noqa: B036 - diagnostics only
            pass
    try:
        source_owner = BaseException.__getattribute__(source, "source_cleanup_owner")
    except AttributeError:
        source_owner = None
    if source_owner is not None:
        try:
            existing_owner = BaseException.__getattribute__(
                target,
                "source_cleanup_owner",
            )
        except AttributeError:
            existing_owner = None
        if existing_owner is None:
            try:
                BaseException.__setattr__(
                    target,
                    "source_cleanup_owner",
                    source_owner,
                )
            except BaseException:  # noqa: B036 - diagnostics only
                pass
        elif existing_owner is not source_owner:
            try:
                merged_owner = _SourceCleanupGroup(existing_owner, source_owner)
                BaseException.__setattr__(
                    target,
                    "source_cleanup_owner",
                    merged_owner,
                )
            except BaseException:  # noqa: B036 - exact callback stays primary
                pass


def _interitem_cancellation(
    values: Iterable[_SecurityItem],
    check_cancelled: Callable[[], None] | None,
) -> Iterator[_SecurityItem]:
    """Poll after each completed item only when another item may follow."""

    if check_cancelled is None:
        yield from values
        return
    if type(values) in _TRUSTED_SIZED_ITERABLE_TYPES:
        value_count: int | None = len(values)
        iterator = iter(values)
    else:
        check_cancelled()
        iterator = iter(values)
        check_cancelled()
        value_count = None
    for index, value in enumerate(iterator):
        yield value
        if value_count is None or index + 1 < value_count:
            check_cancelled()


def _mapping_items_interruptibly(
    values: Mapping[Any, _SecurityItem],
    check_cancelled: Callable[[], None] | None,
) -> Iterator[tuple[Any, _SecurityItem]]:
    """Traverse mapping items without combining a future key and value read."""

    if check_cancelled is None:
        yield from values.items()
        return
    if type(values) is dict:
        yield from _interitem_cancellation(values.items(), check_cancelled)
        return
    check_cancelled()
    keys = values.keys()
    check_cancelled()
    iterator = iter(keys)
    check_cancelled()
    while True:
        try:
            key = next(iterator)
        except StopIteration:
            return
        check_cancelled()
        value = values[key]
        yield key, value
        check_cancelled()


def _interruptible_sorted_security_items(
    values: Sequence[_SecurityItem],
    *,
    key: Callable[[_SecurityItem], object] | None,
    check_cancelled: Callable[[], None] | None,
) -> tuple[_SecurityItem, ...]:
    """Sort bounded stable runs while keeping cancellation inter-item."""

    if check_cancelled is None:
        return tuple(sorted(values, key=key))
    value_count = len(values)
    runs: list[list[_SecurityItem]] = []
    for start in range(0, value_count, _SECURITY_SORT_RUN_ENTRIES):
        end = min(start + _SECURITY_SORT_RUN_ENTRIES, value_count)
        run = list(values[start:end])
        run.sort(key=key)
        runs.append(run)
        if end < value_count:
            check_cancelled()
    if not runs:
        return ()
    while len(runs) > 1:
        check_cancelled()
        merged_runs: list[list[_SecurityItem]] = []
        run_count = len(runs)
        for run_index in range(0, run_count, 2):
            left = runs[run_index]
            if run_index + 1 == run_count:
                merged_runs.append(left)
            else:
                right = runs[run_index + 1]
                merged: list[_SecurityItem] = []
                merged_count = len(left) + len(right)
                for index, value in enumerate(heapq.merge(left, right, key=key)):
                    merged.append(value)
                    if index + 1 < merged_count:
                        check_cancelled()
                merged_runs.append(merged)
            if run_index + 2 < run_count:
                check_cancelled()
        runs = merged_runs
    return tuple(_interitem_cancellation(runs[0], check_cancelled))


def _contains_pattern(
    value: Any,
    patterns: Sequence[Any],
    *,
    check_cancelled: Callable[[], None] | None,
) -> bool:
    """Match one pattern sequence with bounded text scans and no terminal poll."""

    if check_cancelled is None:
        return any(pattern in value for pattern in patterns)

    def contains_large_pattern(current: Any, needle: Any) -> bool:
        prefix: list[int] = []
        matched = 0
        needle_length = len(needle)
        for position in range(needle_length):
            character = needle[position]
            while matched and character != needle[matched]:
                matched = prefix[matched - 1]
            if position and character == needle[matched]:
                matched += 1
            prefix.append(matched)
            if (
                position + 1 < needle_length
                and (position + 1) % _SEMANTIC_SCAN_CHARS == 0
            ):
                check_cancelled()

        matched = 0
        current_length = len(current)
        for position in range(current_length):
            character = current[position]
            while matched and character != needle[matched]:
                matched = prefix[matched - 1]
            if character == needle[matched]:
                matched += 1
                if matched == needle_length:
                    return True
            if (
                position + 1 < current_length
                and (position + 1) % _SEMANTIC_SCAN_CHARS == 0
            ):
                check_cancelled()
        return False

    pattern_count = len(patterns)
    for index, pattern in enumerate(patterns):
        compatible = (isinstance(value, str) and isinstance(pattern, str)) or (
            isinstance(value, bytes) and isinstance(pattern, (bytes, bytearray))
        )
        if compatible and pattern and len(value) > _SEMANTIC_SCAN_CHARS:
            maximum_start = len(value) - len(pattern)
            matched = False
            if maximum_start >= 0:
                if len(pattern) > _SEMANTIC_SCAN_CHARS:
                    matched = contains_large_pattern(value, pattern)
                else:
                    for start in range(
                        0,
                        maximum_start + 1,
                        _SEMANTIC_SCAN_CHARS,
                    ):
                        search_end = min(
                            len(value),
                            start + _SEMANTIC_SCAN_CHARS + len(pattern) - 1,
                        )
                        if value.find(pattern, start, search_end) >= 0:
                            matched = True
                            break
                        if start + _SEMANTIC_SCAN_CHARS <= maximum_start:
                            check_cancelled()
        else:
            matched = pattern in value
        if matched:
            return True
        if index + 1 < pattern_count:
            check_cancelled()
    return False


def _sensitive_environment_name(
    name: str,
    *,
    check_cancelled: Callable[[], None] | None,
) -> bool:
    if check_cancelled is None:
        upper = name.upper()
        return upper in _SENSITIVE_ENV_NAMES or upper.endswith(_SENSITIVE_ENV_SUFFIXES)

    longest = max(
        *(len(item) for item in _SENSITIVE_ENV_NAMES),
        *(len(item) for item in _SENSITIVE_ENV_SUFFIXES),
    )
    exact_parts: list[str] | None = []
    upper_length = 0
    suffix = ""
    name_length = len(name)
    for offset in range(0, name_length, _SEMANTIC_SCAN_CHARS):
        end = min(name_length, offset + _SEMANTIC_SCAN_CHARS)
        piece = name[offset:end].upper()
        upper_length += len(piece)
        if exact_parts is not None:
            if upper_length <= longest:
                exact_parts.append(piece)
            else:
                exact_parts = None
        suffix = (suffix + piece)[-longest:]
        if end < name_length:
            check_cancelled()
    exact = None if exact_parts is None else "".join(exact_parts)
    return exact in _SENSITIVE_ENV_NAMES or suffix.endswith(_SENSITIVE_ENV_SUFFIXES)


def _validate_json_complexity_interruptibly(
    value: Any,
    *,
    label: str,
    max_nodes: int,
    max_depth: int,
    max_key_bytes: int,
    check_cancelled: Callable[[], None],
) -> None:
    """Mirror the bounded decoded-tree pass with inter-item polling."""

    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"{label} exceeds its {max_nodes}-node limit")
        if depth > max_depth:
            raise ValueError(f"{label} exceeds its {max_depth}-level depth limit")
        if isinstance(current, dict):
            item_count = len(current)
            for index, (key, child) in enumerate(current.items()):
                if (
                    len(key) > max_key_bytes
                    or len(key.encode("utf-8", errors="surrogatepass")) > max_key_bytes
                ):
                    raise ValueError(
                        f"{label} contains a key exceeding {max_key_bytes} bytes"
                    )
                stack.append((child, depth + 1))
                if index + 1 < item_count:
                    check_cancelled()
        elif isinstance(current, list):
            item_count = len(current)
            for index, child in enumerate(current):
                stack.append((child, depth + 1))
                if index + 1 < item_count:
                    check_cancelled()
        if stack:
            check_cancelled()


def _interruptible_payload_blocks(
    payload: bytes,
    check_cancelled: Callable[[], None],
) -> Iterator[bytes]:
    """Yield bounded payload blocks and never poll after the final block."""

    block_count = (len(payload) + _SCAN_CHUNK_BYTES - 1) // _SCAN_CHUNK_BYTES
    for index, offset in enumerate(range(0, len(payload), _SCAN_CHUNK_BYTES)):
        yield payload[offset : offset + _SCAN_CHUNK_BYTES]
        if index + 1 < block_count:
            check_cancelled()


class _InterruptibleReader:
    __slots__ = ("_source", "_check_cancelled", "_remaining")

    def __init__(self, source: Any, check_cancelled: Callable[[], None]) -> None:
        self._source = source
        self._check_cancelled = check_cancelled
        source_size = getattr(source, "size", None)
        self._remaining = source_size if isinstance(source_size, int) else None

    def read(self, size: int = -1) -> bytes:
        if self._remaining is None or self._remaining > 0:
            self._check_cancelled()
        payload = self._source.read(size)
        if self._remaining is not None:
            self._remaining = max(0, self._remaining - len(payload))
        return payload


def _interruptible_chunks(
    chunks: Iterable[bytes],
    check_cancelled: Callable[[], None] | None,
    *,
    expected_size: int | None = None,
) -> Iterator[bytes]:
    iterator = iter(chunks)
    remaining = expected_size
    while True:
        if check_cancelled is not None and (remaining is None or remaining > 0):
            check_cancelled()
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        if remaining is not None:
            remaining = max(0, remaining - len(chunk))
        yield chunk


def assert_no_credential_fields(
    value: Any,
    *,
    source: str,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Reject credential-shaped keys in metadata intended for publication."""

    if check_cancelled is None:
        assert_no_secret_fields(value, source=source)
        return
    if not callable(check_cancelled):
        raise TypeError("publication cancellation check must be callable")
    assert_no_secret_fields(
        value,
        source=source,
        check_cancelled=check_cancelled,
    )


def _forbidden_path_strings(
    paths: Iterable[Path],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    values: set[str] = set()
    selected_paths = (
        paths
        if check_cancelled is None
        else _interitem_cancellation(paths, check_cancelled)
    )
    for path in selected_paths:
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
    if check_cancelled is None:
        return tuple(sorted(value for value in values if value))
    present: list[str] = []
    for value in _interitem_cancellation(values, check_cancelled):
        if value:
            present.append(value)
    return _interruptible_sorted_security_items(
        present,
        key=None,
        check_cancelled=check_cancelled,
    )


def _assert_publishable_json_value_impl(
    value: Any,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Scan decoded JSON semantics without depending on their source encoding."""

    if check_cancelled is None:
        assert_no_credential_fields(value, source=label)
    else:
        assert_no_credential_fields(
            value,
            source=label,
            check_cancelled=check_cancelled,
        )
    if check_cancelled is None:
        forbidden = _forbidden_path_strings(forbidden_paths)
        secrets: Sequence[str] = tuple(
            value
            for name, value in environ.items()
            if isinstance(value, str)
            and len(value) >= 8
            and (
                name.upper() in _SENSITIVE_ENV_NAMES
                or name.upper().endswith(_SENSITIVE_ENV_SUFFIXES)
            )
        )
    else:
        forbidden = _forbidden_path_strings(
            forbidden_paths,
            check_cancelled=check_cancelled,
        )
        secret_items: list[str] = []
        for name, value in _mapping_items_interruptibly(
            environ,
            check_cancelled,
        ):
            if (
                isinstance(value, str)
                and len(value) >= 8
                and _sensitive_environment_name(
                    name,
                    check_cancelled=check_cancelled,
                )
            ):
                secret_items.append(value)
        secrets = secret_items

    def check_text(text: str) -> None:
        if _contains_pattern(
            text,
            forbidden,
            check_cancelled=check_cancelled,
        ):
            raise ValueError(f"{label} contains an absolute build-machine path")
        if _contains_pattern(
            text,
            secrets,
            check_cancelled=check_cancelled,
        ):
            raise ValueError(f"{label} contains a configured credential")

    def validate_current_scalar(current: Any) -> bool:
        """Validate one current leaf before polling for a future sibling."""

        if isinstance(current, (Mapping, list, tuple)):
            return False
        if isinstance(current, str):
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
        return True

    if check_cancelled is not None:
        check_cancelled()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if check_cancelled is None or type(current) is dict:
                item_count = len(current)
                for index, (key, child) in enumerate(current.items()):
                    if not isinstance(key, str):
                        raise ValueError(f"{label} contains a non-text JSON object key")
                    check_text(key)
                    if check_cancelled is None:
                        stack.append(child)
                    elif not validate_current_scalar(child):
                        stack.append(child)
                    if check_cancelled is not None and index + 1 < item_count:
                        check_cancelled()
            else:
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
                    if not isinstance(key, str):
                        raise ValueError(f"{label} contains a non-text JSON object key")
                    check_text(key)
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


def assert_publishable_json_value(
    value: Any,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Scan decoded JSON while preserving exact callback exception identity."""

    if check_cancelled is None or not callable(check_cancelled):
        _assert_publishable_json_value_impl(
            value,
            forbidden_paths=forbidden_paths,
            environ=environ,
            label=label,
            check_cancelled=check_cancelled,
        )
        return

    iteration_error: StopIteration | StopAsyncIteration | None = None
    iteration_carrier: _CallbackIterationStop | None = None

    def preserve_iteration_stop() -> None:
        nonlocal iteration_error, iteration_carrier
        try:
            check_cancelled()
        except (StopIteration, StopAsyncIteration) as error:
            if error is not iteration_error:
                iteration_error = error
                iteration_carrier = _CallbackIterationStop(error)
            assert iteration_carrier is not None
            raise iteration_carrier from None

    try:
        _assert_publishable_json_value_impl(
            value,
            forbidden_paths=forbidden_paths,
            environ=environ,
            label=label,
            check_cancelled=preserve_iteration_stop,
        )
    except _CallbackIterationStop as failure:
        if failure is not iteration_carrier:
            raise
        _transfer_callback_exception_settlement(failure, failure.error)
        raise failure.error from None


def _serialized_patterns(
    values: Iterable[str],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[_SerializedPattern, ...]:
    if check_cancelled is not None:
        pattern_items: list[_SerializedPattern] = []
        selected_values = _interitem_cancellation(values, check_cancelled)
        from json.encoder import encode_basestring_ascii

        for value in selected_values:
            if not value:
                continue
            raw = bytearray()
            escaped = bytearray()
            encodings_match = True
            value_length = len(value)
            for offset in range(0, value_length, _SEMANTIC_SCAN_CHARS):
                end = min(value_length, offset + _SEMANTIC_SCAN_CHARS)
                current = value[offset:end]
                raw_piece = current.encode("utf-8")
                escaped_piece = encode_basestring_ascii(current)[1:-1].encode("ascii")
                raw.extend(raw_piece)
                escaped.extend(escaped_piece)
                if encodings_match and raw_piece != escaped_piece:
                    encodings_match = False
                if end < value_length:
                    check_cancelled()
            pattern_items.append(
                bytes(raw) if len(raw) <= _SEMANTIC_SCAN_CHARS else raw
            )
            if not encodings_match:
                pattern_items.append(
                    bytes(escaped) if len(escaped) <= _SEMANTIC_SCAN_CHARS else escaped
                )
        # Pattern order is not observable: every caller asks only which policy
        # class matched.  Length ordering retains the legacy fast-path without
        # comparing attacker-sized equal-prefix byte strings.
        return _interruptible_sorted_security_items(
            pattern_items,
            key=lambda item: -len(item),
            check_cancelled=check_cancelled,
        )

    patterns: set[bytes] = set()
    for value in values:
        if not value:
            continue
        patterns.add(value.encode("utf-8"))
        escaped = json.dumps(value, ensure_ascii=True)[1:-1]
        patterns.add(escaped.encode("utf-8"))
    return tuple(sorted(patterns, key=lambda item: (-len(item), item)))


def _secret_values(
    environ: Mapping[str, str],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[_SerializedPattern, ...]:
    values: set[str] | list[str] = set() if check_cancelled is None else []
    selected_items = _mapping_items_interruptibly(environ, check_cancelled)
    for name, value in selected_items:
        sensitive = _sensitive_environment_name(
            name,
            check_cancelled=check_cancelled,
        )
        if sensitive and len(value) >= 8:
            if check_cancelled is None:
                assert isinstance(values, set)
                values.add(value)
            else:
                assert isinstance(values, list)
                values.append(value)
    if check_cancelled is None:
        return _serialized_patterns(values)
    return _serialized_patterns(values, check_cancelled=check_cancelled)


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
    check_cancelled: Callable[[], None] | None = None,
) -> str | None:
    if check_cancelled is None:
        needles: Sequence[tuple[str, bytes]] = tuple(
            (kind, needle)
            for kind, values in (("path", forbidden), ("secret", secrets))
            for needle in values
            if needle
        )
    else:
        needle_items: list[tuple[str, bytes]] = []
        for kind, values in (("path", forbidden), ("secret", secrets)):
            for needle in _interitem_cancellation(values, check_cancelled):
                if needle:
                    needle_items.append((kind, needle))
        needles = needle_items
    if not needles:
        return None
    if check_cancelled is None:
        overlap = max(len(needle) for _kind, needle in needles) - 1
    else:
        largest = 0
        for _kind, needle in _interitem_cancellation(needles, check_cancelled):
            largest = max(largest, len(needle))
        overlap = largest - 1
    tail = b""
    for chunk in blocks:
        window = tail + chunk
        selected_needles = (
            needles
            if check_cancelled is None
            else _interitem_cancellation(needles, check_cancelled)
        )
        for kind, needle in selected_needles:
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


def _capture_publishable_tree_postflight(
    reader: PublicationDirectoryReader,
    *,
    expected: object,
    entry_policy: Callable[[str, str, int, int], None],
    entry_validator: Callable[[str, str, int, int, Callable[[], None] | None], None],
    check_cancelled: Callable[[], None] | None,
) -> None:
    """Reconcile an exact final-capture stop without masking tree changes."""

    if check_cancelled is None:
        reader.capture_ownership(
            entry_policy=entry_policy,
            check_cancelled=None,
        )
        return

    callback_errors: list[BaseException] = []

    def poll() -> None:
        try:
            check_cancelled()
        except BaseException as error:  # noqa: B036 - preserve exact callback fault
            callback_errors.append(error)
            raise

    def tracked_entry_policy(path: str, kind: str, mode: int, size: int) -> None:
        entry_validator(path, kind, mode, size, poll)

    def reconciliation_entry_policy(
        path: str,
        kind: str,
        mode: int,
        size: int,
    ) -> None:
        entry_validator(path, kind, mode, size, None)

    try:
        observed = reader.capture_ownership(
            entry_policy=tracked_entry_policy,
            check_cancelled=poll,
        )
    except BaseException as error:  # noqa: B036 - exact-stop reconciliation
        if not any(error is callback_error for callback_error in callback_errors):
            raise
        try:
            reconciled = reader.capture_ownership(
                entry_policy=reconciliation_entry_policy
            )
        except BaseException as reconciliation_error:  # noqa: B036 - integrity wins
            raise reconciliation_error from error
        if reconciled != expected:
            raise RuntimeError(
                "publishable tree changed during cancellation reconciliation"
            ) from error
        raise
    if observed != expected:
        raise RuntimeError("publishable tree changed during final validation")


def _assert_publishable_tree_reader_interruptibly_impl(
    reader: PublicationDirectoryReader,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    max_json_bytes: int = _MAX_PUBLISHABLE_JSON_BYTES,
    streaming_json_paths: Iterable[str | PurePosixPath] = (),
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Apply publication policy through one authenticated tree authority.

    Publication JSON is UTF-8 without a byte-order mark. Its lexical resource
    budgets are enforced before strict UTF-8 decoding and DOM allocation.
    ``streaming_json_paths`` must enumerate exact JSON paths whose semantic
    validator already consumed their canonical form; those files still receive
    bounded lexical validation and a complete path/credential byte scan here.
    """

    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("publication cancellation check must be callable")
    callback_failures: list[tuple[int, BaseException]] = []
    callback_generation = 0
    if check_cancelled is not None:
        untracked_check_cancelled = check_cancelled

        def tracked_check_cancelled() -> None:
            nonlocal callback_generation
            try:
                untracked_check_cancelled()
            except BaseException as failure:  # noqa: B036 - exact provenance
                callback_generation += 1
                callback_failures.append((callback_generation, failure))
                raise

        check_cancelled = tracked_check_cancelled

    def is_tracked_callback_failure(
        failure: BaseException,
        *,
        after_generation: int,
    ) -> bool:
        return any(
            generation > after_generation and current is failure
            for generation, current in callback_failures
        )

    @contextmanager
    def open_authenticated_file(
        relative: str,
        *,
        max_bytes: int,
    ) -> Iterator[Any]:
        """Let authenticated cleanup finish before rethrowing an exact stop."""

        entered_generation = callback_generation
        callback_failure: BaseException | None = None
        try:
            with reader.open_authenticated_file(
                relative,
                max_bytes=max_bytes,
            ) as source:
                try:
                    yield source
                except BaseException as failure:  # noqa: B036 - provenance gate
                    if not is_tracked_callback_failure(
                        failure,
                        after_generation=entered_generation,
                    ):
                        raise
                    callback_failure = failure
                    # Suppress only our tracked callback while the underlying
                    # authenticated context drains and verifies the current
                    # source.  A finalize/integrity failure then wins below.
        except BaseException as authentication_failure:  # noqa: B036
            if callback_failure is not None:
                raise authentication_failure from callback_failure
            raise
        if callback_failure is not None:
            raise callback_failure

    if (
        isinstance(max_json_bytes, bool)
        or not isinstance(max_json_bytes, int)
        or max_json_bytes <= 0
        or max_json_bytes > _MAX_PUBLISHABLE_JSON_BYTES
    ):
        raise ValueError("publishable JSON byte limit is out of bounds")
    streaming_json: set[str] = set()
    streaming_json_items: list[str] = []
    selected_streaming_paths = (
        streaming_json_paths
        if check_cancelled is None
        else _interitem_cancellation(streaming_json_paths, check_cancelled)
    )
    for raw_relative in selected_streaming_paths:
        if isinstance(raw_relative, PurePosixPath):
            raw_text: object = raw_relative.as_posix()
        else:
            raw_text = raw_relative
        if not isinstance(raw_text, str):
            raise ValueError("streaming publication JSON path is invalid")
        try:
            relative = PurePosixPath(raw_text)
            encoded = raw_text.encode("utf-8", errors="strict")
        except ValueError as exc:
            raise ValueError("streaming publication JSON path is invalid") from exc
        normalized = relative.as_posix()
        if (
            not raw_text
            or raw_text != normalized
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in raw_text
            or relative.suffix != ".json"
            or len(encoded) > 4_096
            or len(relative.parts) > 256
            or any(
                ord(character) < 32 or ord(character) == 127 for character in raw_text
            )
        ):
            raise ValueError("streaming publication JSON path is invalid")
        if normalized in streaming_json:
            raise ValueError("streaming publication JSON path is duplicated")
        streaming_json.add(normalized)
        streaming_json_items.append(normalized)
    roots: Iterable[Path]
    if check_cancelled is None:
        roots = tuple(forbidden_paths)
        forbidden = _serialized_patterns(_forbidden_path_strings(roots))
        secrets = _secret_values(environ)
    else:
        roots = list(_interitem_cancellation(forbidden_paths, check_cancelled))
        forbidden = _serialized_patterns(
            _forbidden_path_strings(
                roots,
                check_cancelled=check_cancelled,
            ),
            check_cancelled=check_cancelled,
        )
        secrets = _secret_values(
            environ,
            check_cancelled=check_cancelled,
        )

    def is_json_path(path: str) -> bool:
        return path.casefold().endswith(".json")

    def validate_entry(
        path: str,
        kind: str,
        mode: int,
        size: int,
        cancellation_check: Callable[[], None] | None,
    ) -> None:
        del mode
        encoded = path.encode("utf-8", errors="strict")
        if _contains_pattern(
            encoded,
            secrets,
            check_cancelled=cancellation_check,
        ):
            raise ValueError(f"{label} contains a configured credential in {path}")
        if (
            kind == "file"
            and path in streaming_json
            and size > _MAX_STREAMING_PUBLISHABLE_JSON_BYTES
        ):
            raise ValueError(
                f"{label} streaming JSON file exceeds its "
                f"{_MAX_STREAMING_PUBLISHABLE_JSON_BYTES}-byte limit: {path}"
            )
        if (
            kind == "file"
            and is_json_path(path)
            and path not in streaming_json
            and size > max_json_bytes
        ):
            raise ValueError(
                f"{label} JSON file exceeds its {max_json_bytes}-byte limit: {path}"
            )

    def entry_policy(path: str, kind: str, mode: int, size: int) -> None:
        validate_entry(
            path,
            kind,
            mode,
            size,
            check_cancelled,
        )

    def matching_kind_blocks(blocks: Iterable[bytes]) -> str | None:
        if check_cancelled is None:
            return _matching_kind_blocks(
                blocks,
                forbidden=forbidden,
                secrets=secrets,
            )
        return _matching_kind_blocks(
            blocks,
            forbidden=forbidden,
            secrets=secrets,
            check_cancelled=check_cancelled,
        )

    ownership = reader.capture_ownership(
        entry_policy=entry_policy,
        check_cancelled=check_cancelled,
    )
    records = directory_ownership_file_records(ownership)
    if check_cancelled is None:
        missing_streaming_json = streaming_json - {record.path for record in records}
        if missing_streaming_json:
            raise ValueError(
                "streaming publication JSON path is absent: "
                f"{sorted(missing_streaming_json)[0]}"
            )
    else:
        record_paths: set[str] = set()
        for record in _interitem_cancellation(records, check_cancelled):
            record_paths.add(record.path)
        ordered_streaming_json = _interruptible_sorted_security_items(
            streaming_json_items,
            key=None,
            check_cancelled=check_cancelled,
        )
        for relative in _interitem_cancellation(
            ordered_streaming_json,
            check_cancelled,
        ):
            if relative not in record_paths:
                raise ValueError(
                    "streaming publication JSON path is absent: " f"{relative}"
                )
    selected_records = (
        records
        if check_cancelled is None
        else _interitem_cancellation(records, check_cancelled)
    )
    for record in selected_records:
        relative = record.path
        if relative in streaming_json:
            json_label = f"{label} JSON {relative}"
            with open_authenticated_file(
                relative,
                max_bytes=_MAX_STREAMING_PUBLISHABLE_JSON_BYTES,
            ) as source:
                # The semantic owner applies per-element budgets. This pass is
                # intentionally lexical-only, with counts bounded by the file
                # size so a legitimate large array is not forced into one DOM.
                lexical_budget = max(1, record.size)
                validate_bounded_json_stream(
                    (
                        source
                        if check_cancelled is None
                        else _InterruptibleReader(source, check_cancelled)
                    ),
                    label=json_label,
                    max_bytes=_MAX_STREAMING_PUBLISHABLE_JSON_BYTES,
                    max_nodes=lexical_budget,
                    max_lexical_tokens=lexical_budget,
                    max_depth=_MAX_PUBLISHABLE_JSON_DEPTH,
                    max_key_bytes=_MAX_PUBLISHABLE_JSON_KEY_BYTES,
                    max_string_bytes=_MAX_PUBLISHABLE_JSON_STRING_BYTES,
                    max_atom_bytes=_MAX_PUBLISHABLE_JSON_ATOM_BYTES,
                )
            with open_authenticated_file(
                relative,
                max_bytes=record.size,
            ) as source:
                match = matching_kind_blocks(
                    _interruptible_chunks(
                        source.iter_bytes(chunk_size=_SCAN_CHUNK_BYTES),
                        check_cancelled,
                        expected_size=source.size,
                    ),
                )
        elif is_json_path(relative):
            json_label = f"{label} JSON {relative}"
            with open_authenticated_file(
                relative,
                max_bytes=max_json_bytes,
            ) as source:
                validate_bounded_json_stream(
                    (
                        source
                        if check_cancelled is None
                        else _InterruptibleReader(source, check_cancelled)
                    ),
                    label=json_label,
                    max_bytes=max_json_bytes,
                    max_nodes=_MAX_PUBLISHABLE_JSON_NODES,
                    max_lexical_tokens=_MAX_PUBLISHABLE_JSON_TOKENS,
                    max_depth=_MAX_PUBLISHABLE_JSON_DEPTH,
                    max_key_bytes=_MAX_PUBLISHABLE_JSON_KEY_BYTES,
                    max_string_bytes=_MAX_PUBLISHABLE_JSON_STRING_BYTES,
                    max_atom_bytes=_MAX_PUBLISHABLE_JSON_ATOM_BYTES,
                )
            payload_buffer = bytearray()
            with open_authenticated_file(
                relative,
                max_bytes=max_json_bytes,
            ) as source:
                for chunk in _interruptible_chunks(
                    source.iter_bytes(chunk_size=_SCAN_CHUNK_BYTES),
                    check_cancelled,
                    expected_size=source.size,
                ):
                    payload_buffer.extend(chunk)
            payload = bytes(payload_buffer)
            payload_blocks: Iterable[bytes] = (
                (payload,)
                if check_cancelled is None
                else _interruptible_payload_blocks(payload, check_cancelled)
            )
            match = matching_kind_blocks(payload_blocks)
            callback_failure: BaseException | None = None
            if check_cancelled is not None:
                try:
                    check_cancelled()
                except BaseException as failure:  # noqa: B036 - exact provenance
                    callback_failure = failure
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
            if callback_failure is not None:
                raise callback_failure
            if check_cancelled is None:
                validate_json_complexity(
                    decoded,
                    label=json_label,
                    max_nodes=_MAX_PUBLISHABLE_JSON_NODES,
                    max_depth=_MAX_PUBLISHABLE_JSON_DEPTH,
                    max_key_bytes=_MAX_PUBLISHABLE_JSON_KEY_BYTES,
                )
            else:
                _validate_json_complexity_interruptibly(
                    decoded,
                    label=json_label,
                    max_nodes=_MAX_PUBLISHABLE_JSON_NODES,
                    max_depth=_MAX_PUBLISHABLE_JSON_DEPTH,
                    max_key_bytes=_MAX_PUBLISHABLE_JSON_KEY_BYTES,
                    check_cancelled=check_cancelled,
                )
            if check_cancelled is None:
                assert_publishable_json_value(
                    decoded,
                    forbidden_paths=roots,
                    environ=environ,
                    label=json_label,
                )
            else:
                assert_publishable_json_value(
                    decoded,
                    forbidden_paths=roots,
                    environ=environ,
                    label=json_label,
                    check_cancelled=check_cancelled,
                )
        else:
            with open_authenticated_file(
                relative,
                max_bytes=record.size,
            ) as source:
                match = matching_kind_blocks(
                    _interruptible_chunks(
                        source.iter_bytes(chunk_size=_SCAN_CHUNK_BYTES),
                        check_cancelled,
                        expected_size=source.size,
                    ),
                )
        if match == "path":
            raise ValueError(
                f"{label} contains an absolute build-machine path in {relative}"
            )
        if match == "secret":
            raise ValueError(f"{label} contains a configured credential in {relative}")
    _capture_publishable_tree_postflight(
        reader,
        expected=ownership,
        entry_policy=entry_policy,
        entry_validator=validate_entry,
        check_cancelled=check_cancelled,
    )


def _assert_publishable_tree_reader_interruptibly(
    reader: PublicationDirectoryReader,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    max_json_bytes: int = _MAX_PUBLISHABLE_JSON_BYTES,
    streaming_json_paths: Iterable[str | PurePosixPath] = (),
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Preserve iteration sentinels through streaming policy generators."""

    if check_cancelled is None or not callable(check_cancelled):
        _assert_publishable_tree_reader_interruptibly_impl(
            reader,
            forbidden_paths=forbidden_paths,
            environ=environ,
            label=label,
            max_json_bytes=max_json_bytes,
            streaming_json_paths=streaming_json_paths,
            check_cancelled=check_cancelled,
        )
        return

    iteration_error: StopIteration | StopAsyncIteration | None = None
    iteration_carrier: _CallbackIterationStop | None = None

    def preserve_iteration_stop() -> None:
        nonlocal iteration_error, iteration_carrier
        try:
            check_cancelled()
        except (StopIteration, StopAsyncIteration) as error:
            if error is not iteration_error:
                iteration_error = error
                iteration_carrier = _CallbackIterationStop(error)
            assert iteration_carrier is not None
            raise iteration_carrier from None

    try:
        _assert_publishable_tree_reader_interruptibly_impl(
            reader,
            forbidden_paths=forbidden_paths,
            environ=environ,
            label=label,
            max_json_bytes=max_json_bytes,
            streaming_json_paths=streaming_json_paths,
            check_cancelled=preserve_iteration_stop,
        )
    except _CallbackIterationStop as failure:
        if failure is not iteration_carrier:
            raise
        _transfer_callback_exception_settlement(failure, failure.error)
        raise failure.error from None


def assert_publishable_tree_reader(
    reader: PublicationDirectoryReader,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    max_json_bytes: int = _MAX_PUBLISHABLE_JSON_BYTES,
    streaming_json_paths: Iterable[str | PurePosixPath] = (),
) -> None:
    """Apply publication policy through one authenticated tree authority."""

    _assert_publishable_tree_reader_interruptibly(
        reader,
        forbidden_paths=forbidden_paths,
        environ=environ,
        label=label,
        max_json_bytes=max_json_bytes,
        streaming_json_paths=streaming_json_paths,
    )


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
