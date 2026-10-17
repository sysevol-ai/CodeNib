"""Late source compilation preserves real source without model or label inputs."""

import hashlib
import json

import pytest

from scripts import contextbench_late_packet as late
from scripts.contextbench_packet_reader import validate_packet


def read(file, text, identifier):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": identifier,
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({"file_path": file}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": identifier, "content": text},
    ]


def test_late_packet_is_bounded_novel_and_version_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(late.native, "audit_contextbench_snapshot", lambda *a: {})
    for file in ("a.py", "b.py"):
        (tmp_path / file).write_text("".join(f"{file}_line_{n}\n" for n in range(300)))
    messages = read("a.py", "1 | a.py_line_0", "a") + read(
        "b.py", "1 | b.py_line_0", "b"
    )
    messages.append({"role": "assistant", "content": "Investigate b.py first."})
    result = late.compile_packets(
        repo=tmp_path,
        commit="pinned",
        messages=messages,
        extra_tokens=256,
        total_tokens=1024,
    )
    extra = json.loads(result["extra_context"])
    assert extra[0]["file"] == "b.py"
    assert 1 not in [
        int(line.split(" | ", 1)[0]) for line in extra[0]["content"].splitlines()
    ]
    assert 0 < result["extra_tokens"] <= 256
    assert all(n <= 1024 for n in result["context_tokens"].values())
    for context in result["contexts"].values():
        assert validate_packet(tmp_path, context, result["source_sha256"])[
            "source_lines"
        ]
    assert (
        result["source_sha256"]["a.py"]
        == hashlib.sha256((tmp_path / "a.py").read_bytes()).hexdigest()
    )


def test_handoff_cannot_introduce_unobserved_files(tmp_path, monkeypatch):
    monkeypatch.setattr(late.native, "audit_contextbench_snapshot", lambda *a: {})
    (tmp_path / "a.py").write_text("known\nunread\n")
    (tmp_path / "secret.py").write_text("UNOBSERVED\n")
    messages = read("a.py", "1 | known", "a")
    messages.append({"role": "assistant", "content": "Read secret.py or ../outside.py"})
    result = late.compile_packets(repo=tmp_path, commit="pinned", messages=messages)
    assert set(result["source_sha256"]) == {"a.py"}
    assert "UNOBSERVED" not in result["extra_context"]


def test_source_change_rejects_packet(tmp_path, monkeypatch):
    monkeypatch.setattr(late.native, "audit_contextbench_snapshot", lambda *a: {})
    (tmp_path / "a.py").write_text("known\nunread\n")
    tile = late.source_windows

    def mutate(lines):
        result = tile(lines)
        (tmp_path / "a.py").write_text("changed\nunread\n")
        return result

    monkeypatch.setattr(late, "source_windows", mutate)
    with pytest.raises(ValueError, match="version changed"):
        late.compile_packets(
            repo=tmp_path, commit="pinned", messages=read("a.py", "1 | known", "a")
        )


@pytest.mark.parametrize(
    "literal", ["<|endoftext|>", "<|fim_prefix|>", "<|endofprompt|>"]
)
def test_source_tokenizer_markers_remain_literal_data(tmp_path, monkeypatch, literal):
    """Transformers source previously aborted packing before any consumer call."""
    import tiktoken

    monkeypatch.setattr(late.native, "audit_contextbench_snapshot", lambda *a: {})
    (tmp_path / "tokens.py").write_text(f"known\ntoken = {literal!r}\n")
    messages = read("tokens.py", "1 | known", "known")
    messages.append(
        {"role": "assistant", "content": "tokens.py contains the definition"}
    )
    result = late.compile_packets(repo=tmp_path, commit="pinned", messages=messages)
    context = result["contexts"]["late_physical4096"]
    assert literal in context
    assert (
        validate_packet(tmp_path, context, result["source_sha256"])["source_lines"] == 2
    )
    assert result["context_tokens"]["late_physical4096"] == len(
        tiktoken.get_encoding("cl100k_base").encode(context, disallowed_special=())
    )
