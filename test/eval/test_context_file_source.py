"""Real-source delivery and bounded first-read expansion, without live calls."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import tiktoken

from scripts import contextbench_file_source as windows
from scripts.benchmark_context_agent import contextbench_prediction


def view(repo, history, *, tokens=2048, checkpoint=lambda data: None):
    return (
        windows.FileSourceView(repo=repo, token_budget=tokens, checkpoint=checkpoint),
        lambda: history,
    )


def test_read_preserves_source_and_credits_only_returned_unread_lines(tmp_path):
    source = "def primary():\n    return 1\n\ndef helper():\n    return 'NEW'\n"
    (tmp_path / "a.py").write_text(source)
    args = {"file_path": "a.py", "offset": 1, "limit": 1}
    original = "1 | def primary():"
    history = windows.read_messages({}, "a.py:2:     return 1")
    history[0]["tool_calls"][0]["function"]["name"] = "grep"
    source_view, observe = view(tmp_path, history)
    read = source_view.wrap(
        lambda original=original, **kwargs: original, observed_messages=observe
    )
    reply = read(**args)
    assert reply.startswith(original + windows.MARKER) and "NEW" in reply
    trace = source_view.ledger["views"][0]
    assert trace["added_lines"] == [3, 4, 5]
    assert trace["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    prediction = contextbench_prediction(
        instance_id="case",
        messages=history + windows.read_messages(args, reply),
        repo_path=tmp_path,
    )
    assert prediction["traj_data"]["pred_spans"] == {"a.py": [{"start": 1, "end": 5}]}
    history.extend(windows.read_messages(args, reply))
    assert read(**args) == original and len(source_view.ledger["views"]) == 1


def test_partial_current_read_allows_bounded_whole_source_lines(tmp_path):
    source = "".join(f"source_line_{n} = {n}\n" for n in range(1000))
    (tmp_path / "a.py").write_text(source)
    original = "1 | source_line_0 = 0\n2 | source_lin" + "x" * 15900
    source_view, observe = view(tmp_path, [], tokens=256)
    read = source_view.wrap(
        lambda original=original, **kwargs: original, observed_messages=observe
    )
    reply = read(file_path="a.py")
    assert reply.startswith(original + windows.MARKER)
    trace = source_view.ledger["views"][0]
    assert trace["added_lines"] and 2 in trace["added_lines"]
    suffix = reply[len(original) :]
    assert len(tiktoken.get_encoding("cl100k_base").encode(suffix)) <= 256
    assert len(reply) <= windows.MAX_REPLY_CHARS
    actual = contextbench_prediction(
        instance_id="case",
        messages=windows.read_messages({"file_path": "a.py"}, reply),
        repo_path=tmp_path,
    )["traj_data"]["pred_spans"]["a.py"]
    assert sum(span["end"] - span["start"] + 1 for span in actual) == (
        1 + len(trace["added_lines"])
    )


def test_window_scan_limits_and_unchanged_error_or_oversized_reply(tmp_path):
    lines = ["x\n"] * 64 + ["y" * 3001 + "\n", "last\n"]
    assert windows.source_windows(lines) == ([(0, 64), (65, 66)], [65])
    (tmp_path / "a.py").write_text("visible\nmore\n")
    for original in ("Error: missing", "1 | visible\n" + "x" * 32000):
        source_view, observe = view(tmp_path, [])
        read = source_view.wrap(
            lambda original=original, **kwargs: original, observed_messages=observe
        )
        assert read(file_path="a.py") == original
        assert not source_view.ledger["failed"]
    (tmp_path / "a.py").write_text("visible\n" + "x" * (2 * 1024**2))
    source_view, observe = view(tmp_path, [])
    read = source_view.wrap(lambda **kwargs: "1 | visible", observed_messages=observe)
    assert read(file_path="a.py") == "1 | visible"
    assert source_view.ledger["views"] == []


def test_changed_source_stops_selection_and_records_failure(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_text("visible\nunread\n")
    tile = windows.source_windows

    def changed(lines):
        result = tile(lines)
        path.write_text("changed\nunread\n")
        return result

    monkeypatch.setattr(windows, "source_windows", changed)
    source_view, observe = view(tmp_path, [])
    read = source_view.wrap(lambda **kwargs: "1 | visible", observed_messages=observe)
    with pytest.raises(ValueError, match="Source changed"):
        read(file_path="a.py")
    assert source_view.ledger == {"views": [], "failed": True}


def test_simultaneous_first_reads_publish_both_receipts(tmp_path):
    (tmp_path / "a.py").write_text("visible\nunread\n")
    barrier = Barrier(2)
    checkpoints = []

    def execute(**arguments):
        barrier.wait(timeout=5)
        return "1 | visible"

    source_view, observe = view(
        tmp_path,
        [],
        checkpoint=lambda ledger: checkpoints.append(copy.deepcopy(ledger)),
    )
    read = source_view.wrap(execute, observed_messages=observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: read(file_path="a.py"), range(2)))
    assert replies[0] == replies[1] and "2 | unread" in replies[0]
    assert [len(item["views"]) for item in checkpoints] == [1, 2]
    assert len(source_view.ledger["views"]) == 2
