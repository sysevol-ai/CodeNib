# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import math
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codenib._bounded_json import (
    canonical_json_array_chunks,
    iter_bounded_json_array,
    validate_bounded_json_stream,
)


class _ChunkReader:
    def __init__(self, payload: bytes, chunk_size: int) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.offset = 0

    def read(self, _size: int = -1) -> bytes:
        block = self.payload[self.offset : self.offset + self.chunk_size]
        self.offset += len(block)
        return block


@pytest.mark.parametrize(
    "payload",
    [b'{"object":0}', b"[0,1]", b'"scalar"', b"7", b"true", b"null"],
)
def test_bounded_json_stream_accepts_arbitrary_top_level_values(
    payload: bytes,
) -> None:
    validate_bounded_json_stream(io.BytesIO(payload), label="publication JSON")


@pytest.mark.parametrize("block", [bytearray(b"{}"), "{}", None])
def test_bounded_json_stream_rejects_non_bytes_reader_blocks(block: object) -> None:
    class InvalidReader:
        def read(self, _size: int = -1) -> object:
            return block

    with pytest.raises(ValueError, match="invalid or oversized block"):
        validate_bounded_json_stream(InvalidReader(), label="publication JSON")


def test_bounded_json_stream_rejects_oversized_reader_blocks() -> None:
    class OversizedReader:
        def read(self, size: int = -1) -> bytes:
            assert size > 0
            return b" " * (size + 1)

    with pytest.raises(ValueError, match="invalid or oversized block"):
        validate_bounded_json_stream(OversizedReader(), label="publication JSON")


def test_bounded_json_stream_enforces_total_byte_limit() -> None:
    with pytest.raises(ValueError, match="4-byte limit"):
        validate_bounded_json_stream(
            io.BytesIO(b'"1234"'),
            label="publication JSON",
            max_bytes=4,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"value":[', "truncated JSON"),
        (b'{"value":[]]', "object value without a delimiter"),
        (b"{} {}", "trailing data"),
    ],
)
def test_bounded_json_stream_rejects_invalid_framing(
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_bounded_json_stream(io.BytesIO(payload), label="publication JSON")


def test_bounded_json_stream_handles_strings_escapes_and_keys_across_blocks() -> None:
    block_bytes = 1024 * 1024
    long_string = b'"' + (b"s" * (block_bytes + 17)) + b'"'
    escaped_string = b'"' + (b"e" * (block_bytes - 2)) + b"\\" + b'n-tail"'
    long_key = b'{"' + (b"k" * (block_bytes + 17)) + b'":0}'
    payloads = [
        (long_string, {"max_key_bytes": 16}),
        (escaped_string, {"max_key_bytes": 16}),
        (long_key, {"max_key_bytes": block_bytes + 17}),
    ]

    for payload, limits in payloads:
        validate_bounded_json_stream(
            io.BytesIO(payload),
            label="publication JSON",
            max_bytes=len(payload),
            max_string_bytes=len(payload),
            **limits,
        )

    with pytest.raises(ValueError, match="string exceeds"):
        validate_bounded_json_stream(
            io.BytesIO(long_string),
            label="publication JSON",
            max_bytes=len(long_string),
            max_string_bytes=block_bytes + 16,
        )
    with pytest.raises(ValueError, match="key exceeding"):
        validate_bounded_json_stream(
            io.BytesIO(long_key),
            label="publication JSON",
            max_bytes=len(long_key),
            max_string_bytes=len(long_key),
            max_key_bytes=block_bytes + 16,
        )


def test_bounded_array_handles_every_chunk_boundary() -> None:
    values = [
        123,
        {"content": 'é\\"😀', "values": [True, None, -0.0, 1e-10]},
        "tail",
    ]
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()

    for chunk_size in range(1, len(payload) + 1):
        assert (
            list(
                iter_bounded_json_array(
                    _ChunkReader(payload, chunk_size),
                    label="documents",
                )
            )
            == values
        )


def test_bounded_array_does_not_accept_a_number_prefix_at_a_chunk_end() -> None:
    assert list(
        iter_bounded_json_array(
            _ChunkReader(b"[123,4]", 3),
            label="documents",
        )
    ) == [123, 4]


def test_bounded_array_attests_final_trailing_data_before_yield() -> None:
    values = iter_bounded_json_array(
        io.BytesIO(b'[{"page_content":"x"}]TRAIL'),
        label="documents",
    )

    with pytest.raises(ValueError, match="trailing data"):
        next(values)


def test_bounded_array_attests_buffered_trailing_comma_before_yield() -> None:
    values = iter_bounded_json_array(
        io.BytesIO(b'[{"page_content":"x"},]'),
        label="documents",
    )

    with pytest.raises(ValueError, match="trailing JSON array comma"):
        next(values)


@pytest.mark.parametrize("first_block", [b"[0]", b"[0,"])
def test_bounded_array_does_not_read_future_block_before_yield(
    first_block: bytes,
) -> None:
    class PoisonedTailReader:
        calls = 0

        def read(self, _size: int = -1) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return first_block
            raise AssertionError("array parser consumed a future source block")

    source = PoisonedTailReader()
    values = iter_bounded_json_array(source, label="documents")

    assert next(values) == 0
    assert source.calls == 1


@pytest.mark.parametrize("block", [bytearray(b"[]"), "[]", None])
def test_bounded_array_rejects_non_bytes_reader_blocks(block: object) -> None:
    class InvalidReader:
        def read(self, _size: int = -1) -> object:
            return block

    with pytest.raises(ValueError, match="invalid or oversized block"):
        list(iter_bounded_json_array(InvalidReader(), label="documents"))


def test_bounded_array_rejects_oversized_reader_blocks() -> None:
    class OversizedReader:
        def read(self, size: int = -1) -> bytes:
            assert size > 0
            return b" " * (size + 1)

    with pytest.raises(ValueError, match="invalid or oversized block"):
        list(iter_bounded_json_array(OversizedReader(), label="documents"))


@pytest.mark.parametrize(
    "payload",
    [
        b'[{"a":1,"\\u0061":2}]',
        b"[NaN]",
        b"[Infinity]",
        b"[1e9999]",
        b"[" + b"1" * 1_025 + b"]",
    ],
)
def test_bounded_array_rejects_ambiguous_or_unbounded_numbers(payload: bytes) -> None:
    with pytest.raises(ValueError):
        list(iter_bounded_json_array(io.BytesIO(payload), label="documents"))


def test_bounded_array_enforces_complexity_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._bounded_json as bounded_json

    decode_called = False

    class _ForbiddenDecoder:
        def __init__(self, **_kwargs) -> None:
            pass

        def raw_decode(self, _value: str, _offset: int):
            nonlocal decode_called
            decode_called = True
            raise AssertionError("lexical budget must reject before DOM allocation")

    monkeypatch.setattr(bounded_json.json, "JSONDecoder", _ForbiddenDecoder)
    with pytest.raises(ValueError, match="token limit"):
        list(
            bounded_json.iter_bounded_json_array(
                io.BytesIO(b"[[" + b",".join(b"0" for _ in range(20)) + b"]]"),
                label="documents",
                max_lexical_tokens=10,
            )
        )
    assert decode_called is False


@pytest.mark.parametrize(
    ("payload", "limits", "message"),
    [
        (b"[[0,1,2]]", {"max_nodes_per_element": 3}, "node limit"),
        (b"[[[[0]]]]", {"max_depth": 2}, "depth limit"),
    ],
)
def test_node_and_depth_budgets_reject_before_dom_allocation(
    payload: bytes,
    limits: dict[str, int],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._bounded_json as bounded_json

    decode_called = False

    class _ForbiddenDecoder:
        def __init__(self, **_kwargs) -> None:
            pass

        def raw_decode(self, _value: str, _offset: int):
            nonlocal decode_called
            decode_called = True
            raise AssertionError("structural budget must reject before decoding")

    monkeypatch.setattr(bounded_json.json, "JSONDecoder", _ForbiddenDecoder)
    with pytest.raises(ValueError, match=message):
        list(
            bounded_json.iter_bounded_json_array(
                io.BytesIO(payload),
                label="documents",
                **limits,
            )
        )
    assert decode_called is False


def test_depth_budget_uses_the_same_one_based_levels_before_and_after_decode() -> None:
    assert list(
        iter_bounded_json_array(
            io.BytesIO(b"[[[0]]]"),
            label="documents",
            max_depth=3,
        )
    ) == [[[0]]]


def test_item_budget_rejects_the_next_element_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._bounded_json as bounded_json

    real_decoder = json.JSONDecoder
    decode_calls = 0

    class _TrackingDecoder:
        def __init__(self, **kwargs) -> None:
            self._decoder = real_decoder(**kwargs)

        def raw_decode(self, value: str, offset: int):
            nonlocal decode_calls
            decode_calls += 1
            return self._decoder.raw_decode(value, offset)

    monkeypatch.setattr(bounded_json.json, "JSONDecoder", _TrackingDecoder)
    with pytest.raises(ValueError, match="1-item limit"):
        list(
            bounded_json.iter_bounded_json_array(
                io.BytesIO(b"[0,1]"),
                label="documents",
                max_items=1,
            )
        )
    assert decode_calls == 1


def test_item_budget_rejects_current_continuation_before_yield() -> None:
    values = iter_bounded_json_array(
        io.BytesIO(b"[0,1]"),
        label="documents",
        max_items=1,
    )

    with pytest.raises(ValueError, match="1-item limit"):
        next(values)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xef\xbb\xbf[]",
        b"[0,]",
        b"[0,,1]",
        b"[{]",
        b"[[)]",
        b'["\\x"]',
        b'["\x01"]',
        b"[0] trailing",
    ],
)
def test_bounded_array_rejects_malformed_framing(payload: bytes) -> None:
    with pytest.raises(ValueError):
        list(iter_bounded_json_array(io.BytesIO(payload), label="documents"))


def test_escape_dense_string_scanning_scales_linearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._bounded_json as bounded_json

    real_pattern = bounded_json._STRING_SPECIAL

    class _TrackingStringSpecial:
        def __init__(self) -> None:
            self.search_calls = 0
            self.scanned_bytes = 0

        def search(self, block: bytes, offset: int):
            self.search_calls += 1
            match = real_pattern.search(block, offset)
            end = match.start() + 1 if match is not None else len(block)
            self.scanned_bytes += end - offset
            return match

    tracker = _TrackingStringSpecial()
    monkeypatch.setattr(bounded_json, "_STRING_SPECIAL", tracker)

    bytes_method_calls: list[str] = []
    previous_profile = sys.getprofile()
    previous_profile_disable = getattr(previous_profile, "disable", None)
    previous_profile_enable = getattr(previous_profile, "enable", None)
    if previous_profile is not None and not callable(previous_profile):
        if not callable(previous_profile_disable) or not callable(
            previous_profile_enable
        ):
            raise AssertionError("active profiler cannot be restored safely")
        previous_profile_disable()

    def track_bytes_methods(frame, event: str, argument: object) -> None:
        if event == "c_call" and type(getattr(argument, "__self__", None)) is bytes:
            method_name = getattr(argument, "__name__", None)
            if isinstance(method_name, str):
                bytes_method_calls.append(method_name)
        if callable(previous_profile):
            previous_profile(frame, event, argument)

    pairs = 10_000
    payload = b'["' + (b"\\\\" * pairs) + b'"]'
    sys.setprofile(track_bytes_methods)
    try:
        observed = list(
            bounded_json.iter_bounded_json_array(
                _ChunkReader(payload, 257),
                label="escape-dense documents",
                max_element_bytes=len(payload),
            )
        )
    finally:
        sys.setprofile(None)
        if callable(previous_profile):
            sys.setprofile(previous_profile)
        elif previous_profile is not None:
            assert callable(previous_profile_enable)
            previous_profile_enable()

    # The regex accounts for the complete scan. No independent bytes method may
    # rescan a block on every escape; odd blocks also exercise split pairs.
    assert observed == ["\\" * pairs]
    assert tracker.search_calls == pairs + 1
    assert tracker.scanned_bytes == pairs + 1
    assert bytes_method_calls == []


def test_canonical_array_chunks_match_the_existing_json_contract() -> None:
    values = [
        {},
        {"z": [1, {"é": "line\n😀"}], "a": -0.0},
        [True, False, None, math.nextafter(1.0, 2.0)],
        "\ud800",
    ]
    observed = b"".join(canonical_json_array_chunks(iter(values)))
    expected = (
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("ascii")

    assert observed == expected


def _pipeline_rss(path: Path) -> dict[str, int]:
    script = textwrap.dedent(
        """
        import hashlib
        import json
        import os
        import sys

        from codenib._bounded_json import (
            canonical_json_array_chunks,
            iter_bounded_json_array,
        )

        def vm_hwm_kib():
            with open("/proc/self/status", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1])
            raise RuntimeError("VmHWM is unavailable")

        before = vm_hwm_kib()
        count = 0
        largest_chunk = 0
        digest = hashlib.sha256()

        def values():
            global count
            with open(sys.argv[1], "rb") as source:
                for value in iter_bounded_json_array(
                    source,
                    label="documents",
                ):
                    count += 1
                    yield value

        for chunk in canonical_json_array_chunks(values()):
            largest_chunk = max(largest_chunk, len(chunk))
            digest.update(chunk)
        print(
            json.dumps(
                {
                    "before_kib": before,
                    "after_kib": vm_hwm_kib(),
                    "count": count,
                    "largest_chunk": largest_chunk,
                    "input_bytes": os.path.getsize(sys.argv[1]),
                }
            )
        )
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the isolated RSS evidence reads Linux VmHWM",
)
def test_streaming_pipeline_peak_rss_is_bounded_by_one_element(
    tmp_path: Path,
) -> None:
    many = tmp_path / "many.json"
    item = b'{"metadata":{},"page_content":"' + (b"x" * 8_160) + b'"}'
    target_bytes = 32 * 1024 * 1024
    with many.open("wb") as handle:
        handle.write(b"[")
        size = 1
        count = 0
        while size + len(item) + 2 < target_bytes:
            if count:
                handle.write(b",")
                size += 1
            handle.write(item)
            size += len(item)
            count += 1
        handle.write(b"]")

    many_stats = _pipeline_rss(many)
    assert many_stats["count"] == count
    assert many_stats["after_kib"] - many_stats["before_kib"] < 64 * 1024
    assert many_stats["largest_chunk"] <= 1024 * 1024

    near_limit = tmp_path / "near-limit.json"
    with near_limit.open("wb") as handle:
        handle.write(b'["')
        block = b"x" * (1024 * 1024)
        for _ in range(60):
            handle.write(block)
        handle.write(b'"]')

    element_stats = _pipeline_rss(near_limit)
    assert element_stats["count"] == 1
    assert element_stats["after_kib"] - element_stats["before_kib"] < 256 * 1024
    assert element_stats["largest_chunk"] <= 1024 * 1024
