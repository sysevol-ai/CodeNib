# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from filelock import FileLock

from codenib.dataset.contextbench import ContextBenchCase
from codenib.eval.benchmarks import contextbench_runner as runner
from codenib.eval.benchmarks._contextbench_evaluate import validate_symbol_extraction


def test_symbol_probe_rejects_silently_empty_parser_results():
    # Reproduces the official extractor's swallowed bytes/str TypeError.
    with pytest.raises(RuntimeError, match="known-function probe"):
        validate_symbol_extraction(lambda _path: [])


def test_parser_preflight_surfaces_actionable_error_before_loading_tasks(monkeypatch):
    monkeypatch.setattr(runner, "contextbench_evaluator_command", lambda *_: ["probe"])

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            1, ["probe"], stderr="known-function probe failed; use --evaluator-python"
        )

    monkeypatch.setattr(runner.subprocess, "run", fail)
    args = SimpleNamespace(
        command="evaluate", contextbench_root=None, evaluator_python="python"
    )
    with pytest.raises(RuntimeError, match="known-function probe failed"):
        runner._run(args)


@pytest.fixture
def checkout(tmp_path, request):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    filename, content = getattr(request, "param", ("a.py", "def f():\n    return 1\n"))
    (repo / filename).write_text(content)
    (repo / ".gitignore").write_text("ignored.py\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return (
        repo,
        subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip(),
    )


def task(commit, instance_id="task"):
    return ContextBenchCase.from_mapping(
        {
            "instance_id": instance_id,
            "original_inst_id": "owner__repo-1",
            "repo": "owner/repo",
            "base_commit": commit,
            "language": "python",
            "source": "Verified",
            "problem_statement": "Find f",
            "gold_context": [{"file": "a.py", "start_line": 1, "end_line": 2}],
        }
    )


@pytest.mark.parametrize(
    "mutation", ["tracked", "untracked", "ignored", "hidden", "revision"]
)
def test_snapshot_rejects_mutation(checkout, mutation):
    repo, commit = checkout
    assert runner.audit_contextbench_snapshot(repo, commit)["clean"]
    if mutation == "tracked":
        (repo / "a.py").write_text("changed")
    elif mutation in ("untracked", "ignored"):
        (repo / f"{mutation}.py").write_text("unexpected")
    elif mutation == "hidden":
        subprocess.run(
            ["git", "-C", str(repo), "update-index", "--assume-unchanged", "a.py"],
            check=True,
        )
    else:
        commit = "b" * 40
    with pytest.raises(ValueError, match="snapshot"):
        runner.audit_contextbench_snapshot(repo, commit)


def test_bound_tool_does_not_change_cwd_or_allow_escape(checkout):
    repo, _ = checkout
    calls = []
    bound = runner._bind_source_tool(
        lambda **kw: calls.append(kw), repo=repo, path_argument="file_path"
    )
    bound(file_path="a.py", limit=2)
    assert calls == [{"file_path": str(repo / "a.py"), "limit": 2}]
    with pytest.raises(ValueError, match="escapes"):
        bound(file_path="../secret")


def test_search_subdirectory_returns_repository_relative_paths(checkout):
    from codenib.agent.tools.defaults import _grep

    repo, _ = checkout
    (repo / "src").mkdir()
    (repo / "src" / "nested.py").write_text("return_value = 1\n")
    bound = runner._bind_source_tool(_grep, repo=repo, path_argument="path")
    assert "src/nested.py:1:" in bound(path="src", pattern="return_value")


def test_official_failures_and_missing_predictions_stay_in_report(
    monkeypatch, tmp_path
):
    cases = [task("a" * 40), task("a" * 40, "missing")]
    monkeypatch.setattr(runner, "prepare_contextbench_repo", lambda *_: tmp_path)

    def evaluate(command, **kwargs):
        path = command[command.index("--output") + 1]
        from pathlib import Path

        Path(path).write_text(
            json.dumps(
                {
                    "cases": [{"instance_id": "task", "error": "no_context_extracted"}],
                    "summary": {"num_total": 1, "num_valid": 0},
                }
            )
        )
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runner.subprocess, "run", evaluate)
    result = runner.evaluate_contextbench(
        cases=cases,
        predictions=[{"instance_id": "task", "traj_data": {}}],
        repos_root=tmp_path,
        output=tmp_path,
        command=["evaluator"],
    )
    assert result["summary"]["num_total"] == 2
    assert result["summary"]["num_valid"] == 0
    assert result["summary"]["failure_ids"] == ["task", "missing"]


def test_run_requires_live_and_openrouter_before_any_work(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        runner.main(["run", "--output", str(tmp_path)])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        runner.main(["run", "--live", "--output", str(tmp_path)])


def test_resume_checks_config_and_lock_prevents_double_work(monkeypatch, tmp_path):
    cases = (task("a" * 40),)
    monkeypatch.setattr(runner, "load_contextbench_cases", lambda *_a, **_kw: cases)
    monkeypatch.setattr(runner, "prepare_contextbench_repo", lambda *_: tmp_path)
    monkeypatch.setattr(
        runner, "audit_contextbench_snapshot", lambda *_: {"clean": True}
    )
    args = ["prepare", "--output", str(tmp_path)]
    assert runner.main(args) == 0
    assert runner.main(args) == 1
    assert runner.main([*args, "--resume"]) == 0
    assert runner.main([*args, "--resume", "--top-k", "9"]) == 1
    with FileLock(str(tmp_path / ".run.lock"), timeout=0):
        assert runner.main([*args, "--resume"]) == 1


def test_evaluator_does_not_accept_stale_results(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "prepare_contextbench_repo", lambda *_: tmp_path)
    (tmp_path / "metrics.json").write_text('{"summary":{"num_valid":1}}')
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=1)
    )
    with pytest.raises(RuntimeError, match="evaluation failed"):
        runner.evaluate_contextbench(
            cases=[task("a" * 40)],
            predictions=[{"instance_id": "task"}],
            repos_root=tmp_path,
            output=tmp_path,
            command=["evaluator"],
        )


@pytest.mark.parametrize(
    "checkout",
    [
        ("a.py", "def f():\n    return 1\n"),
        ("a.js", "function f() {\n  return 1;\n}\n"),
        ("a.ts", "function f(): number {\n  return 1;\n}\n"),
        ("a.go", "package main\nfunc f() int {\n return 1\n}\n"),
        ("a.c", "int f(void) {\n return 1;\n}\n"),
        ("a.cpp", "int f() {\n return 1;\n}\n"),
        ("a.rs", "fn f() -> i32 {\n 1\n}\n"),
        ("A.java", "class A {\n int f() { return 1; }\n}\n"),
    ],
    indirect=True,
)
def test_native_agent_pipeline_keeps_gold_out_of_prompts(
    monkeypatch, checkout, tmp_path
):
    from codenib.llm.litellm_chat import LiteLLMChat

    repo, commit = checkout
    source = next(
        path for path in repo.iterdir() if path.is_file() and path.name != ".gitignore"
    )
    observed = []

    def completion(_self, messages, **kwargs):
        observed.extend(messages)
        turn = sum(m["role"] == "tool" for m in messages)
        if turn < 2:
            name = "bm25_search" if turn == 0 else "read"
            args = (
                {"query": "f return", "top_k": 1}
                if turn == 0
                else {"file_path": source.name}
            )
            function = SimpleNamespace(name=name, arguments=json.dumps(args))
            call = SimpleNamespace(id=f"call{turn}", function=function)
            call.model_dump = lambda **_: {
                "id": call.id,
                "type": "function",
                "function": vars(function),
            }
            message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        else:
            message = SimpleNamespace(
                role="assistant",
                content="The function f is in a.py:1-2.",
                tool_calls=[],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(LiteLLMChat, "_call_raw", completion)
    result = runner.run_contextbench_agent(
        task(commit),
        repo=repo,
        cache_dir=tmp_path / "index",
        model="openrouter/openai/gpt-4.1-mini",
        max_turns=3,
        max_tokens=128,
        top_k=1,
        timeout=5,
        skills=("bm25_search",),
    )
    assert result["prediction"]["traj_data"]["pred_spans"][source.name] == [
        {"start": 1, "end": len(source.read_text().splitlines())}
    ]
    assert result["prediction"]["traj_data"]["pred_steps"][0]["spans"][source.name]
    assert result["total_turns"] == 3
    assert "gold_context" not in json.dumps(observed)
    assert runner.audit_contextbench_snapshot(repo, commit)["clean"]


@pytest.mark.integration
@pytest.mark.parametrize("with_patch", [False, True])
def test_official_evaluator_scores_native_trajectory(checkout, tmp_path, with_patch):
    import sys
    from pathlib import Path

    upstream = os.environ.get("CONTEXTBENCH_CHECKOUT")
    if not upstream:
        pytest.skip("set CONTEXTBENCH_CHECKOUT to the pinned official checkout")
    repo, commit = checkout
    case = task(commit)
    root = tmp_path / "repos"
    root.mkdir()
    repo.rename(root / case.instance_id)
    prediction = {
        "instance_id": case.original_inst_id,
        "traj_data": {
            "pred_steps": [
                {"files": ["a.py"], "spans": {"a.py": [{"start": 1, "end": 2}]}},
                {"files": ["a.py"], "spans": {"a.py": [{"start": 1, "end": 1}]}},
            ],
            "pred_files": ["a.py"],
            "pred_spans": {"a.py": [{"start": 1, "end": 2}]},
        },
    }
    if with_patch:
        prediction["model_patch"] = (
            "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n"
            " def f():\n-    return 1\n+    return 2\n"
        )
    report = runner.evaluate_contextbench(
        cases=[case],
        predictions=[prediction],
        repos_root=root,
        output=tmp_path,
        command=runner.contextbench_evaluator_command(
            Path(upstream),
            os.environ.get("CONTEXTBENCH_EVALUATOR_PYTHON", sys.executable),
        ),
    )
    assert report["summary"]["num_valid"] == 1
    assert report["cases"][0]["final"]["symbol"]["gold_size"] == 1
    assert report["cases"][0]["final"]["symbol"]["pred_size"] == 1
    for granularity in ("file", "symbol", "span", "line"):
        assert report["summary"][f"final_{granularity}"] == {
            "coverage": 1.0,
            "precision": 1.0,
        }
    if with_patch:
        # Official micro recall counts one edited line in two gold lines.
        assert report["summary"]["editloc"]["recall"] == 0.5
        assert report["summary"]["editloc"]["precision"] == 1.0
        assert (root / case.instance_id / "a.py").read_text() == (
            "def f():\n    return 1\n"
        )
    else:
        assert "editloc" not in report["summary"]
    assert report["summary"]["traj_redundancy_file"] > 0


@pytest.mark.integration
def test_official_extractors_find_definitions_in_all_benchmark_languages(tmp_path):
    import sys

    upstream = os.environ.get("CONTEXTBENCH_CHECKOUT")
    if not upstream:
        pytest.skip("set CONTEXTBENCH_CHECKOUT to the pinned official checkout")
    sources = {
        "a.py": "def f():\n    return 1\n",
        "a.js": "function f() { return 1; }\n",
        "a.ts": "function f(): number { return 1; }\n",
        "a.go": "package main\nfunc f() int { return 1 }\n",
        "a.c": "int f(void) { return 1; }\n",
        "a.cpp": "int f() { return 1; }\n",
        "a.rs": "fn f() -> i32 { 1 }\n",
        "A.java": "class A { int f() { return 1; } }\n",
    }
    for name, content in sources.items():
        (tmp_path / name).write_text(content)
    command = [
        os.environ.get("CONTEXTBENCH_EVALUATOR_PYTHON", sys.executable),
        "-c",
        "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from contextbench.extractors import extract_defs; "
        "print(json.dumps({p.name:len(extract_defs(str(p))) "
        "for p in Path(sys.argv[2]).iterdir()}))",
        upstream,
        str(tmp_path),
    ]
    counts = json.loads(subprocess.check_output(command, text=True, timeout=60))
    assert counts.keys() == sources.keys()
    assert all(count > 0 for count in counts.values())
