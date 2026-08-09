# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Incremental, complexity-bounded parsing for large JSON document arrays."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO, Protocol

_READ_BYTES = 1024 * 1024
DEFAULT_MAX_ARRAY_ITEMS = 1_000_000
DEFAULT_MAX_ELEMENT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_NODES_PER_ELEMENT = 100_000
DEFAULT_MAX_LEXICAL_TOKENS = 200_000
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_KEY_BYTES = 4_096


class BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def validate_json_complexity(
    value: Any,
    *,
    label: str,
    max_nodes: int = DEFAULT_MAX_NODES_PER_ELEMENT,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_key_bytes: int = DEFAULT_MAX_KEY_BYTES,
) -> None:
    """Reject one decoded value before recursive consumers can exhaust resources."""

    # Depth is one-based in both the lexical framer and decoded-tree check: the
    # element itself is level one and its direct children are level two.
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
            for key, child in current.items():
                if len(key.encode("utf-8", errors="surrogatepass")) > max_key_bytes:
                    raise ValueError(
                        f"{label} contains a key exceeding {max_key_bytes} bytes"
                    )
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


class _ByteStream:
    def __init__(self, source: BinaryReader) -> None:
        self._source = source
        self._block = b""
        self._position = 0
        self._eof = False
        self.offset = 0

    def current_block(self) -> tuple[bytes, int] | None:
        """Return the unread block slice without a Python call per byte."""

        while self._position >= len(self._block):
            if self._eof:
                return None
            self._block = self._source.read(_READ_BYTES)
            self._position = 0
            if not self._block:
                self._eof = True
                return None
        return self._block, self._position

    def advance(self, count: int) -> None:
        if count < 0 or self._position + count > len(self._block):
            raise AssertionError("invalid bounded JSON stream advance")
        self._position += count
        self.offset += count

    def next(self) -> int | None:
        current = self.current_block()
        if current is None:
            return None
        block, position = current
        value = block[position]
        self.advance(1)
        return value


_JSON_WHITESPACE = frozenset(b" \t\r\n")
_ATOM_DELIMITERS = frozenset(b" \t\r\n,]}:")


def _next_non_whitespace(stream: _ByteStream) -> int | None:
    while (value := stream.next()) is not None:
        if value not in _JSON_WHITESPACE:
            return value
    return None


def _frame_json_element(
    stream: _ByteStream,
    first: int,
    *,
    label: str,
    max_element_bytes: int,
    max_tokens: int,
    max_depth: int,
) -> tuple[bytearray, int]:
    """Frame one complete array element before allocating its decoded DOM."""

    payload = bytearray()
    stack: list[int] = []
    in_string = False
    escaped = False
    string_bytes = 0
    in_atom = False
    atom_bytes = 0
    token_count = 0
    first_pending = True

    def reserve(count: int) -> None:
        if len(payload) + count > max_element_bytes:
            raise ValueError(f"{label} element exceeds {max_element_bytes} bytes")

    while True:
        if first_pending:
            # ``first`` was already consumed by _next_non_whitespace.
            block = bytes((first,))
            position = 0
            first_pending = False
            synthetic = True
        else:
            current_block = stream.current_block()
            if current_block is None:
                raise ValueError(f"{label} is truncated JSON")
            block, position = current_block
            synthetic = False
        index = position
        while index < len(block):
            current = block[index]

            if in_string:
                if escaped:
                    escaped = False
                    string_bytes += 1
                    index += 1
                    continue
                quote = block.find(b'"', index)
                slash = block.find(b"\\", index)
                candidates = tuple(item for item in (quote, slash) if item >= 0)
                special = min(candidates) if candidates else -1
                if special < 0:
                    string_bytes += len(block) - index
                    index = len(block)
                    break
                string_bytes += special - index + 1
                if block[special] == ord("\\"):
                    escaped = True
                else:
                    in_string = False
                index = special + 1
                if string_bytes > max_element_bytes:
                    raise ValueError(
                        f"{label} string exceeds {max_element_bytes} bytes"
                    )
                continue

            if in_atom:
                if current in _ATOM_DELIMITERS:
                    in_atom = False
                    atom_bytes = 0
                else:
                    atom_bytes += 1
                    if atom_bytes > 1_024:
                        raise ValueError(f"{label} number/literal exceeds 1024 bytes")
                    index += 1
                    continue

            if not stack and current in {ord(","), ord("]")}:
                reserve(index - position)
                payload.extend(block[position:index])
                if not synthetic:
                    stream.advance(index - position + 1)
                while payload and payload[-1] in _JSON_WHITESPACE:
                    payload.pop()
                if not payload:
                    raise ValueError(f"{label} contains an empty JSON array element")
                return payload, current

            if current == ord('"'):
                token_count += 1
                in_string = True
                string_bytes = 0
            elif current in {ord("{"), ord("[")}:
                token_count += 1
                stack.append(current)
                if len(stack) > max_depth:
                    raise ValueError(
                        f"{label} exceeds its {max_depth}-level depth limit"
                    )
            elif current in {ord("}"), ord("]")}:
                expected = ord("{") if current == ord("}") else ord("[")
                if not stack or stack[-1] != expected:
                    raise ValueError(f"{label} contains mismatched JSON delimiters")
                stack.pop()
            elif current in b"-0123456789tfn":
                token_count += 1
                in_atom = True
                atom_bytes = 1
            if token_count > max_tokens:
                raise ValueError(f"{label} exceeds its {max_tokens}-token limit")
            index += 1

        reserve(len(block) - position)
        payload.extend(block[position:])
        if not synthetic:
            stream.advance(len(block) - position)
    raise ValueError(f"{label} is truncated JSON")


def _bounded_parse_int(value: str) -> int:
    if len(value) > 1_024:
        raise ValueError("JSON integer exceeds its 1024-character limit")
    return int(value)


def _bounded_parse_float(value: str) -> float:
    if len(value) > 1_024:
        raise ValueError("JSON number exceeds its 1024-character limit")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is not finite")
    return parsed


def iter_bounded_json_array(
    source: BinaryReader,
    *,
    label: str,
    max_items: int = DEFAULT_MAX_ARRAY_ITEMS,
    max_element_bytes: int = DEFAULT_MAX_ELEMENT_BYTES,
    max_nodes_per_element: int = DEFAULT_MAX_NODES_PER_ELEMENT,
    max_lexical_tokens: int = DEFAULT_MAX_LEXICAL_TOKENS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_key_bytes: int = DEFAULT_MAX_KEY_BYTES,
) -> Iterator[Any]:
    """Yield a top-level JSON array without retaining the complete input or DOM."""

    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (
            max_items,
            max_element_bytes,
            max_nodes_per_element,
            max_lexical_tokens,
            max_depth,
            max_key_bytes,
        )
    ):
        raise ValueError("bounded JSON limits must be positive integers")
    stream = _ByteStream(source)
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_object,
        parse_constant=_reject_nonfinite_number,
        parse_int=_bounded_parse_int,
        parse_float=_bounded_parse_float,
    )
    first = _next_non_whitespace(stream)
    if first != ord("["):
        raise ValueError(f"{label} must be a JSON list")
    item_count = 0
    first = _next_non_whitespace(stream)
    if first == ord("]"):
        first = None
    elif first is None:
        raise ValueError(f"{label} is truncated JSON")
    while True:
        if first is None:
            break
        element_offset = stream.offset - 1
        while True:
            try:
                framed, delimiter = _frame_json_element(
                    stream,
                    first,
                    label=label,
                    max_element_bytes=max_element_bytes,
                    max_tokens=max_lexical_tokens,
                    max_depth=max_depth,
                )
                text = framed.decode("utf-8", errors="strict")
                value, end = decoder.raw_decode(text, 0)
                if end != len(text):
                    raise ValueError(f"{label} element contains trailing data")
                del text, framed
                break
            except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ValueError(
                    f"{label} element {item_count} at byte {element_offset} "
                    "contains invalid JSON"
                ) from exc
        validate_json_complexity(
            value,
            label=f"{label} element {item_count}",
            max_nodes=max_nodes_per_element,
            max_depth=max_depth,
            max_key_bytes=max_key_bytes,
        )
        item_count += 1
        if item_count > max_items:
            raise ValueError(f"{label} exceeds its {max_items}-item limit")
        yield value
        if delimiter == ord("]"):
            first = None
        else:
            first = _next_non_whitespace(stream)
            if first is None:
                raise ValueError(f"{label} is truncated JSON")
            if first == ord("]"):
                raise ValueError(f"{label} has a trailing JSON array comma")

    if _next_non_whitespace(stream) is not None:
        raise ValueError(f"{label} contains trailing data")


def _indent(level: int) -> bytes:
    return b"  " * level


def _canonical_string_chunks(value: str) -> Iterator[bytes]:
    from json.encoder import encode_basestring_ascii

    yield b'"'
    for offset in range(0, len(value), 64 * 1024):
        encoded = encode_basestring_ascii(value[offset : offset + 64 * 1024])
        yield encoded[1:-1].encode("ascii")
    yield b'"'


def canonical_json_value_chunks(value: Any, *, level: int = 0) -> Iterator[bytes]:
    """Stream the exact stdlib canonical representation of one decoded value."""

    if value is None:
        yield b"null"
    elif value is True:
        yield b"true"
    elif value is False:
        yield b"false"
    elif isinstance(value, str):
        yield from _canonical_string_chunks(value)
    elif isinstance(value, int):
        yield str(value).encode("ascii")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number is not finite")
        yield repr(value).encode("ascii")
    elif isinstance(value, list):
        if not value:
            yield b"[]"
            return
        yield b"[\n"
        for index, item in enumerate(value):
            if index:
                yield b",\n"
            yield _indent(level + 1)
            yield from canonical_json_value_chunks(item, level=level + 1)
        yield b"\n" + _indent(level) + b"]"
    elif isinstance(value, dict):
        if not value:
            yield b"{}"
            return
        yield b"{\n"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b",\n"
            yield _indent(level + 1)
            yield from _canonical_string_chunks(key)
            yield b": "
            yield from canonical_json_value_chunks(value[key], level=level + 1)
        yield b"\n" + _indent(level) + b"}"
    else:
        raise TypeError(f"unsupported decoded JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return b"".join(canonical_json_value_chunks(value))


def canonical_json_array_chunks(values: Iterable[Any]) -> Iterator[bytes]:
    """Encode values exactly like ``json.dumps(list(values), indent=2, ...)``."""

    iterator = iter(values)
    emitted = False
    try:
        for value in iterator:
            if not emitted:
                yield b"[\n"
                emitted = True
            else:
                yield b",\n"
            yield b"  "
            yield from canonical_json_value_chunks(value, level=1)
        yield b"\n]\n" if emitted else b"[]\n"
    finally:
        close_iterator = getattr(iterator, "close", None)
        if callable(close_iterator):
            close_iterator()


def write_chunks(handle: BinaryIO, chunks: Iterable[bytes]) -> tuple[int, str]:
    """Write chunks while returning their size and SHA-256."""

    import hashlib

    size = 0
    digest = hashlib.sha256()
    for chunk in chunks:
        handle.write(chunk)
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


__all__ = [
    "DEFAULT_MAX_ARRAY_ITEMS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_ELEMENT_BYTES",
    "DEFAULT_MAX_KEY_BYTES",
    "DEFAULT_MAX_NODES_PER_ELEMENT",
    "canonical_json_array_chunks",
    "canonical_json_bytes",
    "canonical_json_value_chunks",
    "iter_bounded_json_array",
    "validate_json_complexity",
    "write_chunks",
]
