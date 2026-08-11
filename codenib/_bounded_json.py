# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Incremental, complexity-bounded framing for UTF-8 JSON documents."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO, Protocol

_READ_BYTES = 1024 * 1024
DEFAULT_MAX_ARRAY_ITEMS = 1_000_000
DEFAULT_MAX_ELEMENT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_NODES_PER_ELEMENT = 100_000
DEFAULT_MAX_LEXICAL_TOKENS = 200_000
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_KEY_BYTES = 4_096
DEFAULT_MAX_ATOM_BYTES = 1_024
DEFAULT_MAX_STRING_BYTES = 64 * 1024 * 1024


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


def validate_bounded_json_stream(
    source: BinaryReader,
    *,
    label: str,
    max_bytes: int = DEFAULT_MAX_ELEMENT_BYTES,
    max_nodes: int = DEFAULT_MAX_NODES_PER_ELEMENT,
    max_lexical_tokens: int = DEFAULT_MAX_LEXICAL_TOKENS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_key_bytes: int = DEFAULT_MAX_KEY_BYTES,
    max_string_bytes: int = DEFAULT_MAX_STRING_BYTES,
    max_atom_bytes: int = DEFAULT_MAX_ATOM_BYTES,
) -> None:
    """Bound arbitrary JSON lexical complexity before allocating its DOM.

    This is deliberately a framing and resource-budget pass, not a text or
    semantic JSON decoder. Framing uses JSON's ASCII structural bytes, so a
    caller accepting publication JSON must strictly decode UTF-8 before its
    subsequent semantic decode. Duplicate keys, non-finite constants, exact
    number syntax, and decoded-string semantics remain the responsibility of
    that decoder. Object keys count as lexical tokens but not value nodes;
    container and scalar values use the same one-based depth and node model as
    :func:`validate_json_complexity`. String and key byte limits count their
    encoded lexical bytes between quotes, including escape syntax; callers may
    additionally enforce decoded-key limits after parsing.
    """

    limits = (
        max_bytes,
        max_nodes,
        max_lexical_tokens,
        max_depth,
        max_key_bytes,
        max_string_bytes,
        max_atom_bytes,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in limits
    ):
        raise ValueError("bounded JSON limits must be positive integers")

    # Frames contain ``[opening delimiter, parser state]``. The state is only
    # as strict as needed to identify keys and value boundaries safely; the
    # caller's decoder remains the JSON grammar authority.
    frames: list[list[Any]] = []
    root_state = "value"
    in_string = False
    string_is_key = False
    escaped = False
    string_bytes = 0
    key_bytes = 0
    in_atom = False
    atom_bytes = 0
    token_count = 0
    node_count = 0
    total_bytes = 0

    def fail(message: str) -> None:
        raise ValueError(f"{label} {message}")

    def add_token() -> None:
        nonlocal token_count
        token_count += 1
        if token_count > max_lexical_tokens:
            fail(f"exceeds its {max_lexical_tokens}-token limit")

    def start_value() -> None:
        nonlocal node_count
        depth = len(frames) + 1
        if depth > max_depth:
            fail(f"exceeds its {max_depth}-level depth limit")
        node_count += 1
        if node_count > max_nodes:
            fail(f"exceeds its {max_nodes}-node limit")
        add_token()

    def finish_value() -> None:
        nonlocal root_state
        if frames:
            frames[-1][1] = "comma_or_end"
        else:
            root_state = "done"

    def expecting_value() -> bool:
        if frames:
            return frames[-1][1] in {"value", "value_or_end"}
        return root_state == "value"

    def start_string(*, key: bool) -> None:
        nonlocal in_string, string_is_key, escaped, string_bytes, key_bytes
        if key:
            add_token()
        else:
            start_value()
        in_string = True
        string_is_key = key
        escaped = False
        string_bytes = 0
        key_bytes = 0

    def start_atom() -> None:
        nonlocal in_atom, atom_bytes
        start_value()
        in_atom = True
        atom_bytes = 0

    while True:
        block = source.read(_READ_BYTES)
        if type(block) is not bytes or len(block) > _READ_BYTES:
            raise ValueError(
                "bounded JSON reader returned an invalid or oversized block"
            )
        if not block:
            break
        total_bytes += len(block)
        if total_bytes > max_bytes:
            fail(f"exceeds its {max_bytes}-byte limit")

        index = 0
        while index < len(block):
            current = block[index]

            if in_string:
                if escaped:
                    escaped = False
                    string_bytes += 1
                    if string_is_key:
                        key_bytes += 1
                    index += 1
                else:
                    match = _STRING_SPECIAL.search(block, index)
                    if match is None:
                        length = len(block) - index
                        string_bytes += length
                        if string_is_key:
                            key_bytes += length
                        index = len(block)
                    else:
                        special = match.start()
                        length = special - index
                        string_bytes += length
                        if string_is_key:
                            key_bytes += length
                        if block[special] == ord("\\"):
                            string_bytes += 1
                            if string_is_key:
                                key_bytes += 1
                            escaped = True
                        else:
                            in_string = False
                            if string_is_key:
                                frames[-1][1] = "colon"
                            else:
                                finish_value()
                        index = special + 1
                if string_bytes > max_string_bytes:
                    fail(f"string exceeds its {max_string_bytes}-byte limit")
                if string_is_key and key_bytes > max_key_bytes:
                    fail(f"contains a key exceeding {max_key_bytes} bytes")
                continue

            if in_atom:
                if current in _ATOM_DELIMITERS:
                    in_atom = False
                    finish_value()
                    continue
                atom_bytes += 1
                if atom_bytes > max_atom_bytes:
                    fail(f"atom exceeds its {max_atom_bytes}-byte limit")
                index += 1
                continue

            if current in _JSON_WHITESPACE:
                match = _NON_JSON_WHITESPACE.search(block, index + 1)
                index = len(block) if match is None else match.start()
                continue

            if not frames and root_state == "done":
                fail("contains trailing data")

            if frames:
                opening, state = frames[-1]
                if opening == ord("{"):
                    if state == "key_or_end":
                        if current == ord("}"):
                            frames.pop()
                            finish_value()
                            index += 1
                            continue
                        if current != ord('"'):
                            fail("contains an invalid object key")
                        start_string(key=True)
                        index += 1
                        continue
                    if state == "colon":
                        if current != ord(":"):
                            fail("contains an object key without a colon")
                        frames[-1][1] = "value"
                        index += 1
                        continue
                    if state == "comma_or_end":
                        if current == ord("}"):
                            frames.pop()
                            finish_value()
                            index += 1
                            continue
                        if current != ord(","):
                            fail("contains an object value without a delimiter")
                        frames[-1][1] = "key_or_end"
                        index += 1
                        continue
                elif state == "comma_or_end":
                    if current == ord("]"):
                        frames.pop()
                        finish_value()
                        index += 1
                        continue
                    if current != ord(","):
                        fail("contains an array value without a delimiter")
                    frames[-1][1] = "value_or_end"
                    index += 1
                    continue
                elif state == "value_or_end" and current == ord("]"):
                    frames.pop()
                    finish_value()
                    index += 1
                    continue

            if not expecting_value():
                fail("contains invalid JSON framing")
            if current == ord('"'):
                start_string(key=False)
            elif current in {ord("{"), ord("[")}:
                start_value()
                state = "key_or_end" if current == ord("{") else "value_or_end"
                frames.append([current, state])
            elif current in {ord("}"), ord("]"), ord(","), ord(":")}:
                fail("contains mismatched JSON delimiters")
            else:
                # Permit any bounded atom here. The strict decoder below this
                # layer remains authoritative for literals, number syntax, and
                # non-finite constants such as NaN/Infinity.
                start_atom()
                continue
            index += 1

    if in_string or escaped or frames:
        fail("is truncated JSON")
    if in_atom:
        finish_value()
    if root_state != "done":
        fail("is empty or truncated JSON")


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
            block = self._source.read(_READ_BYTES)
            if type(block) is not bytes or len(block) > _READ_BYTES:
                raise ValueError(
                    "bounded JSON reader returned an invalid or oversized block"
                )
            self._block = block
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
_STRING_SPECIAL = re.compile(rb'["\\]')
_NON_JSON_WHITESPACE = re.compile(rb"[^ \t\r\n]")


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
    max_nodes: int,
    max_tokens: int,
    max_depth: int,
) -> tuple[bytearray, int]:
    """Frame one complete array element before allocating its decoded DOM."""

    payload = bytearray()
    stack: list[list[Any]] = []
    in_string = False
    string_is_key = False
    escaped = False
    string_bytes = 0
    in_atom = False
    atom_bytes = 0
    token_count = 0
    node_count = 0
    first_pending = True

    def reserve(count: int) -> None:
        if len(payload) + count > max_element_bytes:
            raise ValueError(f"{label} element exceeds {max_element_bytes} bytes")

    def start_value() -> None:
        nonlocal node_count
        # Match ``validate_json_complexity``: the element root is level one,
        # and each containing object/array adds one level for its children.
        depth = len(stack) + 1
        if depth > max_depth:
            raise ValueError(f"{label} exceeds its {max_depth}-level depth limit")
        node_count += 1
        if node_count > max_nodes:
            raise ValueError(f"{label} exceeds its {max_nodes}-node limit")

    def finish_value() -> None:
        if stack:
            stack[-1][1] = "comma_or_end"

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
                match = _STRING_SPECIAL.search(block, index)
                if match is None:
                    string_bytes += len(block) - index
                    index = len(block)
                    break
                special = match.start()
                string_bytes += special - index + 1
                if block[special] == ord("\\"):
                    escaped = True
                else:
                    in_string = False
                    if string_is_key:
                        stack[-1][1] = "colon"
                    else:
                        finish_value()
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
                    finish_value()
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
                string_is_key = bool(
                    stack and stack[-1][0] == ord("{") and stack[-1][1] == "key_or_end"
                )
                if not string_is_key:
                    start_value()
            elif current in {ord("{"), ord("[")}:
                token_count += 1
                start_value()
                state = "key_or_end" if current == ord("{") else "value_or_end"
                stack.append([current, state])
            elif current in {ord("}"), ord("]")}:
                expected = ord("{") if current == ord("}") else ord("[")
                if not stack or stack[-1][0] != expected:
                    raise ValueError(f"{label} contains mismatched JSON delimiters")
                stack.pop()
                finish_value()
            elif current in b"-0123456789tfn":
                token_count += 1
                start_value()
                in_atom = True
                atom_bytes = 1
            elif current == ord(":") and stack and stack[-1][0] == ord("{"):
                stack[-1][1] = "value"
            elif current == ord(",") and stack:
                stack[-1][1] = (
                    "key_or_end" if stack[-1][0] == ord("{") else "value_or_end"
                )
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
        if item_count >= max_items:
            raise ValueError(f"{label} exceeds its {max_items}-item limit")
        element_offset = stream.offset - 1
        framed, delimiter = _frame_json_element(
            stream,
            first,
            label=label,
            max_element_bytes=max_element_bytes,
            max_nodes=max_nodes_per_element,
            max_tokens=max_lexical_tokens,
            max_depth=max_depth,
        )
        try:
            try:
                text = framed.decode("utf-8", errors="strict")
                value, end = decoder.raw_decode(text, 0)
                if end != len(text):
                    raise ValueError(f"{label} element contains trailing data")
            finally:
                del framed
        except (RecursionError, ValueError) as exc:
            raise ValueError(
                f"{label} element {item_count} at byte {element_offset} "
                "contains invalid JSON"
            ) from exc
        del text
        validate_json_complexity(
            value,
            label=f"{label} element {item_count}",
            max_nodes=max_nodes_per_element,
            max_depth=max_depth,
            max_key_bytes=max_key_bytes,
        )
        item_count += 1
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
    "DEFAULT_MAX_ATOM_BYTES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_ELEMENT_BYTES",
    "DEFAULT_MAX_KEY_BYTES",
    "DEFAULT_MAX_LEXICAL_TOKENS",
    "DEFAULT_MAX_NODES_PER_ELEMENT",
    "DEFAULT_MAX_STRING_BYTES",
    "canonical_json_array_chunks",
    "canonical_json_bytes",
    "canonical_json_value_chunks",
    "iter_bounded_json_array",
    "validate_bounded_json_stream",
    "validate_json_complexity",
    "write_chunks",
]
