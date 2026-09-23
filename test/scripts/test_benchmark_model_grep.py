# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import shutil
import sys

import pytest
import requests

from codenib.types import NodeInfo
from scripts.benchmark_model_grep import Planner, execute, interleave, validate_plan


@pytest.mark.parametrize("cost", ["nan", "inf", "-inf", "0", "-1"])
def test_nonfinite_or_nonpositive_cost_is_rejected_before_preparation(
    monkeypatch, capsys, tmp_path, cost
):
    from scripts import benchmark_model_grep as benchmark

    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_model_grep.py",
            "--prepared",
            str(tmp_path / "prepared"),
            "--prebuilt-root",
            str(tmp_path / "repos"),
            "--output",
            str(output),
            f"--max-cost-usd={cost}",
            "--live",
        ],
    )
    monkeypatch.setattr(
        benchmark,
        "prepare",
        lambda _args: pytest.fail("Invalid cost must not reach preparation"),
    )
    with pytest.raises(SystemExit) as error:
        benchmark.main()

    assert error.value.code == 2
    assert "positive and finite" in capsys.readouterr().err
    assert not output.exists()


def test_grep_selects_smallest_visible_chunk_and_interleaves_actions(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("rg is required for local grep execution")
    (tmp_path / "a.py").write_text(
        "def first():\n    needle()\n\ndef second():\n    needle()\n"
    )
    (tmp_path / "b.py").write_text("def alternate():\n    pass\n")
    nodes = [
        NodeInfo(node_id="whole", file="a.py", start_line=0, end_line=4),
        NodeInfo(node_id="first", file="a.py", start_line=0, end_line=1),
        NodeInfo(node_id="second", file="a.py", start_line=3, end_line=4),
        NodeInfo(node_id="alternate", file="b.py", start_line=0, end_line=1),
    ]
    actions = [
        {"pattern": "needle", "glob": "**/*.py", "case_sensitive": True},
        {"pattern": "ALTERNATE", "glob": "**/*.py", "case_sensitive": False},
    ]
    actual, traces = execute(tmp_path, actions, nodes, 2)
    assert [n.node_id for n in actual] == ["first", "alternate"]
    assert traces[0]["match_lines"] == 2
    assert all("error" not in t for t in traces)
    # A shell metacharacter remains regex data and cannot execute a command.
    actions[0]["pattern"] = "needle;touch injected"
    actual, _ = execute(tmp_path, actions[:1], nodes, 100)
    assert not actual
    assert not (tmp_path / "injected").exists()


def test_grep_has_no_hidden_line_credit_or_implicit_bm25_fallback(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("rg is required for local grep execution")
    (tmp_path / "a.py").write_text("visible\nhidden_target\n")
    node = NodeInfo(node_id="visible", file="a.py", start_line=0, end_line=0)
    action = {"pattern": "hidden_target", "glob": "**/*", "case_sensitive": True}
    actual, _ = execute(tmp_path, [action], [node], 100)
    assert not actual
    assert interleave([[node], [node]], 100) == [node]
    action["pattern"] = "[invalid"
    actual, audit = execute(tmp_path, [action], [node], 100)
    assert not actual and audit[0]["error"]


def test_planner_rejects_unbounded_or_untyped_actions():
    valid = {"pattern": "symbol", "glob": "**/*", "case_sensitive": True}
    assert validate_plan({"actions": [valid]}) == [valid]
    with pytest.raises(ValueError, match="1 to 6"):
        validate_plan({"actions": [valid] * 7})
    with pytest.raises(ValueError, match="boolean"):
        validate_plan({"actions": [{**valid, "case_sensitive": "yes"}]})


def test_planner_retains_rate_limit_and_honors_retry_after(monkeypatch):
    import json

    from scripts import benchmark_model_grep as benchmark

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    limited = requests.Response()
    limited.status_code = 429
    limited.headers["Retry-After"] = "45"
    limited._content = b'{"error":{"message":"rate limit"}}'
    success = requests.Response()
    success.status_code = 200
    actions = [{"pattern": "symbol", "glob": "**/*", "case_sensitive": True}]
    success._content = json.dumps(
        {
            "model": "test-model",
            "usage": {"cost": 0.01},
            "choices": [{"message": {"content": json.dumps({"actions": actions})}}],
        }
    ).encode()
    responses = iter([limited, success])
    monkeypatch.setattr(benchmark.requests, "post", lambda *a, **kw: next(responses))
    sleeps = []
    monkeypatch.setattr(benchmark.time, "sleep", sleeps.append)
    planner = Planner("test-model", 1.0)
    result = planner.plan({"issue": "example"})
    assert sleeps == [45]
    assert [r["http_status"] for r in result["attempts"]] == [429, 200]
    assert result["actions"] == actions
    assert planner.cost_usd == 0.01
    assert "test-key" not in json.dumps(result)
