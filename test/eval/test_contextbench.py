# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from codenib.eval.benchmarks.contextbench import contextbench_prediction


def tool_messages(name, args, content, call_id="call"):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def test_export_counts_visible_lines_not_enclosing_ranges(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(["x"] * 100))
    nodes = [
        {
            "file": "a.py",
            "start_line": 1,
            "end_line": 100,
            "content": "2 | x\n3 | x\n... lines omitted ...\n8 | x",
        },
        {"file": "a.py", "start_line": 20, "end_line": 99, "node_name": "name_only"},
    ]
    messages = tool_messages("bm25_search", {"query": "x"}, json.dumps(nodes))
    messages += tool_messages(
        "read",
        {"file_path": "a.py", "offset": 2, "limit": 2000},
        "2 | x\n3 | x\n4 | x\n... (truncated)",
        "read",
    )
    messages += tool_messages(
        "grep", {"pattern": "x"}, "a.py:10: x\na.py:12: x", "grep"
    )
    messages += tool_messages("glob", {"pattern": "*"}, "a.py", "glob")
    messages += tool_messages("read", {"file_path": "a.py"}, "Error: failed", "failed")
    prediction = contextbench_prediction(
        instance_id="task", messages=messages, repo_path=tmp_path
    )
    traj = prediction["traj_data"]
    assert len(traj["pred_steps"]) == 5
    assert traj["pred_steps"][-1] == {"files": [], "spans": {}}
    assert traj["pred_spans"] == {
        "a.py": [
            {"start": 2, "end": 4},
            {"start": 8, "end": 8},
            {"start": 10, "end": 10},
            {"start": 12, "end": 12},
        ]
    }
    assert traj["pred_steps"][0]["spans"]["a.py"][0] == {"start": 2, "end": 3}


def test_truncated_json_keeps_complete_nodes_only(tmp_path):
    (tmp_path / "a.py").write_text("a\nb\nc\n")
    text = (
        '[{"file":"a.py","content":"1 | a\\n2 | b"}, {"file":"a.py", "content":"3 | c'
    )
    messages = tool_messages("bm25_search", {}, text)
    pred = contextbench_prediction(
        instance_id="task", messages=messages, repo_path=tmp_path
    )
    assert pred["traj_data"]["pred_spans"] == {"a.py": [{"start": 1, "end": 2}]}


@pytest.mark.parametrize("reason", ["unavailable", "is_error"])
def test_rejected_tool_keeps_empty_step_without_source_credit(tmp_path, reason):
    (tmp_path / "a.py").write_text("visible\n")
    messages = tool_messages("read", {"file_path": "a.py"}, "1 | visible")
    if reason == "unavailable":
        messages[0]["tool_calls"][0]["function"][reason] = True
    else:
        messages[1][reason] = True
    prediction = contextbench_prediction(
        instance_id="case", messages=messages, repo_path=tmp_path
    )
    assert prediction["traj_data"]["pred_spans"] == {}
    assert prediction["traj_data"]["pred_steps"] == [{"files": [], "spans": {}}]


def test_partial_json_node_recovers_only_complete_delivered_lines(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(["x"] * 20))
    node = {"file": "a.py", "content": '4 | text="\\n"\n5 | next\n12 | incomplete'}
    text = "[" + json.dumps(node)[:-4] + "\n... (truncated)"
    pred = contextbench_prediction(
        instance_id="task",
        repo_path=tmp_path,
        messages=tool_messages("bm25_search", {}, text),
    )
    assert pred["traj_data"]["pred_spans"] == {"a.py": [{"start": 4, "end": 5}]}


def test_malformed_json_key_does_not_invent_context(tmp_path):
    pred = contextbench_prediction(
        instance_id="task",
        repo_path=tmp_path,
        messages=tool_messages("bm25_search", {}, '[{"unterminated'),
    )
    assert pred["traj_data"]["pred_files"] == []


def test_export_rejects_escaped_paths_and_ignores_filename_only_grep(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("secret\n")
    (repo / "link.py").symlink_to(outside)
    messages = tool_messages("read", {"file_path": str(outside)}, "1 | secret")
    messages += tool_messages("read", {"file_path": "link.py"}, "1 | secret", "symlink")
    messages += tool_messages(
        "grep", {"output_mode": "files_with_matches"}, "link.py", "grep"
    )
    assert (
        contextbench_prediction(instance_id="task", messages=messages, repo_path=repo)[
            "traj_data"
        ]["pred_files"]
        == []
    )


def test_orphan_tool_message_is_not_silently_scored(tmp_path):
    with pytest.raises(ValueError, match="originating"):
        contextbench_prediction(
            instance_id="task",
            messages=[{"role": "tool", "content": "1 | x"}],
            repo_path=tmp_path,
        )
