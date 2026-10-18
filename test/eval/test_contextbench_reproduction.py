# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Portable source observations must preserve code and reject altered evidence."""

import copy
import hashlib
import io
import json
import tarfile
import threading

import pytest

from codenib.eval.benchmarks.contextbench import contextbench_prediction
from scripts.contextbench_reproduce_search_pair import (
    acknowledged,
    ordered_replays,
    rebase_history,
    safe_path,
    verify_extracted,
    verify_runtime,
)


def test_relocated_observations_keep_source_literals_and_exact_credit(tmp_path):
    original, local = tmp_path / "original", tmp_path / "local"
    original.mkdir()
    local.mkdir()
    body = f'LOCATION = "{original}/literal"\nreturn_value = 42\n'
    for folder in [original, local]:
        (folder / "example.py").write_text(body)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "read1",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps(
                            {"file_path": str(original / "example.py")}
                        ),
                    },
                },
                {"id": "grep1", "function": {"name": "grep", "arguments": {}}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "read1",
            "content": "1|" + body.splitlines()[0],
        },
        {
            "role": "tool",
            "tool_call_id": "grep1",
            "content": f"{original}/example.py:2: return_value = 42\n",
        },
        {"role": "assistant", "content": "Acknowledged"},
    ]
    saved = copy.deepcopy(messages)
    translated = rebase_history(messages, original, local)
    assert messages == saved
    assert translated[1]["content"] == messages[1]["content"]
    assert str(original / "literal") in translated[1]["content"]
    assert str(local / "example.py") in translated[2]["content"]
    old = contextbench_prediction(
        instance_id="task", messages=messages, repo_path=original
    )
    new = contextbench_prediction(
        instance_id="task", messages=translated, repo_path=local
    )
    assert old == new
    assert old["traj_data"]["pred_spans"] == {"example.py": [{"start": 1, "end": 2}]}


def test_unacknowledged_tail_does_not_acquire_source_credit():
    messages = [
        {"role": "assistant", "content": "Searching"},
        {"role": "tool", "content": "unconfirmed source"},
    ]
    assert acknowledged(messages) == messages[:1]
    assert acknowledged([messages[1]]) == []


@pytest.mark.parametrize("name", ["../outside", "/absolute", "links/escape"])
def test_archive_members_cannot_escape_destination(tmp_path, name):
    folder = tmp_path / "bundle"
    folder.mkdir()
    (folder / "links").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="relative|escapes"):
        safe_path(folder, name)


def test_extracted_member_mutation_is_rejected(tmp_path):
    member = tmp_path / "logs" / "evidence.json"
    member.parent.mkdir()
    member.write_bytes(b"original receipt")
    index = tmp_path / "archive-index.json"
    index.write_text(
        json.dumps(
            {
                "streams": {
                    "logs": {
                        "members": [
                            {
                                "name": "logs/evidence.json",
                                "bytes": member.stat().st_size,
                                "sha256": hashlib.sha256(
                                    member.read_bytes()
                                ).hexdigest(),
                            }
                        ]
                    }
                }
            }
        )
    )
    (tmp_path / "verification.json").write_text(
        json.dumps(
            {
                "archive_index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                "members_roundtrip_verified": 1,
            }
        )
    )
    assert verify_extracted(tmp_path) == 1
    member.write_bytes(b"modified receipt")
    with pytest.raises(AssertionError):
        verify_extracted(tmp_path)


def test_runtime_mutation_rejected_before_any_model_or_evaluator_import(tmp_path):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo("scripts/original.py")
        info.size = 3
        stream.addfile(info, io.BytesIO(b"old"))
    runtime = tmp_path / "runtime"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "scripts/original.py").write_bytes(b"new")
    with pytest.raises(AssertionError):
        verify_runtime(
            archive, runtime, hashlib.sha256(archive.read_bytes()).hexdigest()
        )


def test_parallel_source_completion_keeps_protocol_order():
    second_finished = threading.Event()
    completed = []

    def verify(case):
        if case == 0:
            assert second_finished.wait(timeout=5)
        completed.append(case)
        if case == 1:
            second_finished.set()
        return {"verified_case": case}

    results = list(ordered_replays([0, 1], verify, jobs=2))
    assert completed == [1, 0]
    assert results == [{"verified_case": 0}, {"verified_case": 1}]


def test_parallel_source_failure_prevents_complete_replay_result():
    def verify(case):
        if case == 0:
            raise ValueError("Pinned source snapshot changed")
        return case

    with pytest.raises(ValueError, match="snapshot changed"):
        list(ordered_replays([0, 1], verify, jobs=2))
