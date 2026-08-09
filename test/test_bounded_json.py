# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import math

import pytest

from codenib._bounded_json import canonical_json_array_chunks, iter_bounded_json_array


class _ChunkReader:
    def __init__(self, payload: bytes, chunk_size: int) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.offset = 0

    def read(self, _size: int = -1) -> bytes:
        block = self.payload[self.offset : self.offset + self.chunk_size]
        self.offset += len(block)
        return block


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
