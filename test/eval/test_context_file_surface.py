# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Real parser/source contracts for the source-only on-demand prototype."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source, fingerprint_repository
from scripts.benchmark_context_agent import contextbench_prediction
from scripts.contextbench_file_surface import FileSurfaceContext


def exchange(arguments, reply, call_id):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {
                        "name": "explore_context",
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": reply},
        {"role": "assistant", "content": "received"},
    ]


@pytest.fixture
def source(tmp_path):
    (tmp_path / "a.py").write_text(
        "".join(f"def answer_{i}():\n    return 'BODY_{i}'\n\n" for i in range(12))
    )
    (tmp_path / "config.yml").write_text("feature:\n  enabled: true\n")
    fp = fingerprint_repository(tmp_path)
    ctx = ServerContext.load(
        RepoManifest(
            repo_path=str(tmp_path),
            commit="a" * 40,
            source_fingerprint=fp.value,
            file_count=fp.file_count,
        ),
        views=[],
        source_binding=capture_repository_source(tmp_path),
    )
    try:
        yield ctx
    finally:
        ctx.vector = None
        ctx.close()


def test_real_file_outline_pages_to_exact_function_bodies_without_indexes(source):
    history = []
    tool = FileSurfaceContext(source, observed_messages=lambda: history)
    args = {"file_path": "a.py"}
    first = tool.execute(**args)
    assert "BODY_" not in first
    page1 = json.loads(first)[0]
    assert page1["parser"] == "python" and len(page1["entries"]) == 8
    assert page1["next_cursor"] == 8
    page2 = json.loads(tool.execute(**args, cursor=8))[0]
    assert len(page2["entries"]) == 5 and page2["next_cursor"] is None
    assert len({e["handle"] for e in page1["entries"] + page2["entries"]}) == 13
    history.extend(exchange(args, first, "surface"))
    assert not contextbench_prediction(
        instance_id="case", messages=history, repo_path=source.manifest.repo_path
    )["traj_data"]["pred_spans"]
    target = next(e for e in page2["entries"] if e["symbol"] == "answer_11")
    focused = json.loads(tool.execute(**args, near_line=34))[0]
    assert focused["entries"][0]["symbol"] == "answer_11"
    body_args = {"view": "body", "handle": target["handle"]}
    body = tool.execute(**body_args)
    assert "BODY_11" in json.loads(body)[0]["content"]
    history.extend(exchange(body_args, body, "body"))
    spans = contextbench_prediction(
        instance_id="case", messages=history, repo_path=source.manifest.repo_path
    )["traj_data"]["pred_spans"]
    assert spans == {"a.py": [{"start": 34, "end": 35}]}
    assert json.loads(tool.execute(**body_args))[0]["content"] == ""
    with pytest.raises(ValueError):
        tool.execute(file_path="../outside.py")


def test_non_code_files_offer_an_authenticated_file_handle_and_reject_edits(source):
    tool = FileSurfaceContext(source, observed_messages=lambda: [])
    page = json.loads(tool.execute(file_path="config.yml"))[0]
    assert page["parser"] == "plain_file" and len(page["entries"]) == 1
    args = {"view": "body", "handle": page["entries"][0]["handle"]}
    assert "enabled: true" in json.loads(tool.execute(**args))[0]["content"]
    (Path(source.manifest.repo_path) / "config.yml").write_text("changed: true\n")
    with pytest.raises((ValueError, RuntimeError, OSError)):
        tool.execute(**args)


def test_file_outline_cannot_silently_switch_to_an_indexed_context(source):
    source.vector = SimpleNamespace()
    with pytest.raises(ValueError, match="source-only"):
        FileSurfaceContext(source, observed_messages=lambda: [])
