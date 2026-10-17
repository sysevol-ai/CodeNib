"""Source-bound file navigation, without model calls or unread-source credit."""

import hashlib
import json
from types import SimpleNamespace

import pytest
import tiktoken

from scripts import contextbench_file_map as maps
from scripts.benchmark_context_agent import contextbench_prediction


def messages(arguments, text):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "read",
                    "function": {"name": "read", "arguments": json.dumps(arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "read", "content": text},
    ]


def test_map_exposes_callable_addresses_without_reading_bodies_into_context(tmp_path):
    source = (
        "def primary():\n    return 1\n\ndef sibling():\n    return 'UNREAD_BODY'\n"
    )
    (tmp_path / "a.py").write_text(source)
    args = {"file_path": "a.py", "offset": 1, "limit": 2}
    original = "1 | def primary():\n2 |     return 1"
    history = []
    read = maps.mapped_read(
        lambda **kwargs: original,
        repo=tmp_path,
        token_budget=2048,
        observed_messages=lambda: history,
    )
    reply = read(**args)
    assert reply.startswith(original + maps.MARKER)
    metadata = json.loads(reply.split(maps.MARKER)[1])
    assert ["sibling()", "function", 4, 5] in metadata["symbols"]
    assert metadata["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert "UNREAD_BODY" not in reply
    raw = contextbench_prediction(
        instance_id="case", messages=messages(args, original), repo_path=tmp_path
    )
    mapped = contextbench_prediction(
        instance_id="case", messages=messages(args, reply), repo_path=tmp_path
    )
    assert mapped == raw
    history.extend(messages(args, reply))
    assert read(**args) == original
    # A grep hit alone must not suppress the map on the first actual read.
    history[0]["tool_calls"][0]["function"]["name"] = "grep"
    history[0]["tool_calls"][0]["function"]["arguments"] = "{}"
    history[1]["content"] = "a.py:1: def primary():"
    assert maps.MARKER in read(**args)


def test_map_bounds_metadata_and_preserves_the_original_reply(tmp_path):
    source = "\n".join(f"def function_{i}():\n    return {i}\n" for i in range(100))
    (tmp_path / "a.py").write_text(source)
    original = "1 | def function_0():"
    read = maps.mapped_read(
        lambda **kwargs: original,
        repo=tmp_path,
        token_budget=256,
        observed_messages=lambda: [],
    )
    reply = read(file_path="a.py", offset=1, limit=1)
    suffix = reply[len(original) :]
    assert 0 < len(tiktoken.get_encoding("cl100k_base").encode(suffix)) <= 256
    metadata = json.loads(reply.split(maps.MARKER)[1])
    assert metadata["symbols"] and metadata["omitted_symbols"] > 0
    assert len(metadata["symbols"]) + metadata["omitted_symbols"] == 100


def test_map_rejects_known_namespace_sized_and_unbound_callable_ranges(
    tmp_path, monkeypatch
):
    (tmp_path / "a.cpp").write_text("void real() {}\n" + "\n" * 599)
    chunks = [
        SimpleNamespace(
            node_id="a.cpp:" + name,
            chunk_type="function",
            start_line=0,
            end_line=end,
        )
        for name, end in (("real()", 0), ("real()", 599), ("invented()", 0))
    ]
    monkeypatch.setattr(
        maps,
        "CodeChunker",
        lambda **kwargs: SimpleNamespace(chunk_file=lambda *a, **kw: chunks),
    )
    result = maps.file_symbols(tmp_path, "a.cpp")
    assert result["symbols"] == [("real()", "function", 1, 1)]
    assert result["rejected_ranges"] == 2
    with pytest.raises(ValueError, match="escapes"):
        maps.file_symbols(tmp_path, "../outside.py")


def test_oversized_or_error_reads_do_not_construct_maps(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail("A non-source/oversized reply must not trigger parsing")

    monkeypatch.setattr(maps, "file_symbols", unexpected)
    for original in ("Error: missing file", "x" * maps._MAX_RESULT_CHARS):
        read = maps.mapped_read(
            lambda original=original, **kwargs: original,
            repo=tmp_path,
            token_budget=2048,
            observed_messages=lambda: [],
        )
        assert read(file_path="a.py") == original
