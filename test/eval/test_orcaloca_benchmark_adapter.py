# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for OrcaLoca's generic localization benchmark adapter."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

from codenib.clients.orcaloca_agent import (
    OrcaLocaAgent,
    OrcaLocaExecution,
    orcaloca_locations_to_baseline,
    resolve_orcaloca_manifest,
)
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.eval.agent_runner.batch import run_baseline_batch
from codenib.eval.baseline import BaselineTask
from codenib.eval.benchmarks.orcaloca import OrcaLocaLocation
from codenib.paths import repo_index_dir
from codenib.source_fingerprint import fingerprint_repository


def _write_manifest(repo, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    RepoManifest(
        repo_path=str(repo.resolve()),
        source_fingerprint=fingerprint_repository(repo).value,
    ).save(path)
    return path


def test_adapter_import_does_not_require_orcaloca_or_llama_index() -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import sys
sys.modules["Orcar"] = None
sys.modules["llama_index"] = None
sys.modules["litellm"] = None
sys.modules["openai"] = None
from codenib.clients.orcaloca_agent import OrcaLocaAgent
assert OrcaLocaAgent.__name__ == "OrcaLocaAgent"
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_locations_translate_to_canonical_generic_symbols() -> None:
    locations = orcaloca_locations_to_baseline(
        [
            OrcaLocaLocation("src/service.py", "Service", "run"),
            OrcaLocaLocation("src/helpers.py", method_name="parse(value)"),
            OrcaLocaLocation("src/model.py", class_name="Model"),
            OrcaLocaLocation("src/settings.py"),
            OrcaLocaLocation("src/service.py", "Service", "run"),
        ]
    )

    assert [(item.file_path, item.name, item.type) for item in locations] == [
        ("src/service.py", "Service.run()", "method"),
        ("src/helpers.py", "parse()", "function"),
        ("src/model.py", "Model", "class"),
        ("src/settings.py", "", "file"),
    ]


def test_default_manifest_resolution_uses_codenib_home(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    expected = _write_manifest(
        repo,
        repo_index_dir(repo) / MANIFEST_FILENAME,
    )

    assert resolve_orcaloca_manifest(repo) == expected.resolve()


def test_agent_implements_generic_locate_code_contract(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = _write_manifest(repo, tmp_path / "repo_manifest.json")
    calls = []

    def run_search(problem_statement, repo_path, manifest_path):
        calls.append((problem_statement, repo_path, manifest_path))
        return OrcaLocaExecution(
            raw_output={
                "bug_locations": [
                    {
                        "file_path": "src/service.py",
                        "class_name": "Service",
                        "method_name": "run",
                    }
                ]
            },
            usage={"input_tokens": 123},
            tool_calls=4,
        )

    agent = OrcaLocaAgent(
        model="test-model",
        manifest_path=manifest,
        search_runner=run_search,
    )
    result = asyncio.run(
        agent.locate_code(
            "Fix the service",
            str(repo),
            {"issue_body": "duplicate context is not appended"},
        )
    )

    assert result.success is True
    assert result.repo_path == str(repo.resolve())
    assert result.usage == {"input_tokens": 123, "tool_calls": 4}
    assert [location.symbol_id() for location in result.locations] == [
        "src/service.py:Service.run()"
    ]
    assert calls == [("Fix the service", str(repo.resolve()), manifest.resolve())]


def test_agent_rejects_manifest_for_another_checkout(tmp_path) -> None:
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other"
    repo.mkdir()
    other_repo.mkdir()
    manifest = _write_manifest(other_repo, tmp_path / "repo_manifest.json")
    called = False

    def run_search(*_args):
        nonlocal called
        called = True
        return {"bug_locations": []}

    agent = OrcaLocaAgent(
        model="test-model",
        manifest_path=manifest,
        search_runner=run_search,
    )
    result = asyncio.run(agent.locate_code("Find the bug", str(repo)))

    assert result.success is False
    assert "does not match" in (result.error_message or "")
    assert called is False


def test_agent_rejects_manifest_for_another_checkout_revision(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    source = repo / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    indexed_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = tmp_path / "repo_manifest.json"
    RepoManifest(repo_path=str(repo.resolve()), commit=indexed_commit).save(manifest)

    source.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True)
    called = False

    def run_search(*_args):
        nonlocal called
        called = True
        return {"bug_locations": []}

    agent = OrcaLocaAgent(
        model="test-model",
        manifest_path=manifest,
        search_runner=run_search,
    )
    result = asyncio.run(agent.locate_code("Find the bug", str(repo)))

    assert result.success is False
    assert "revision does not match" in (result.error_message or "")
    assert called is False


def test_agent_rejects_manifest_for_changed_non_git_source(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = _write_manifest(repo, tmp_path / "repo_manifest.json")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    called = False

    def run_search(*_args):
        nonlocal called
        called = True
        return {"bug_locations": []}

    agent = OrcaLocaAgent(
        model="test-model",
        manifest_path=manifest,
        search_runner=run_search,
    )
    result = asyncio.run(agent.locate_code("Find the bug", str(repo)))

    assert result.success is False
    assert "fingerprint does not match" in (result.error_message or "")
    assert called is False


def test_default_runner_forwards_openai_compatible_base_url(
    monkeypatch, tmp_path
) -> None:
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["llm"] = kwargs

    class FakeSearchInput:
        def __init__(self, *, problem_statement, trace_analysis_output):
            self.problem_statement = problem_statement
            self.trace_analysis_output = trace_analysis_output

        def get_content(self):
            return self.problem_statement

    class FakeTraceAnalysisOutput:
        pass

    class FakeSearchAgent:
        def __init__(self, **kwargs):
            captured["agent"] = kwargs
            self._search_manager = types.SimpleNamespace(history=[])

        def chat(self, content):
            captured["content"] = content
            return types.SimpleNamespace(response='{"bug_locations": []}')

    llama_index = types.ModuleType("llama_index")
    llama_index_llms = types.ModuleType("llama_index.llms")
    llama_index_openai = types.ModuleType("llama_index.llms.openai")
    llama_index_openai.OpenAI = FakeOpenAI
    orcar = types.ModuleType("Orcar")
    orcar_search_agent = types.ModuleType("Orcar.search_agent")
    orcar_search_agent.SearchAgent = FakeSearchAgent
    orcar_types = types.ModuleType("Orcar.types")
    orcar_types.SearchInput = FakeSearchInput
    orcar_types.TraceAnalysisOutput = FakeTraceAnalysisOutput
    for name, module in {
        "llama_index": llama_index,
        "llama_index.llms": llama_index_llms,
        "llama_index.llms.openai": llama_index_openai,
        "Orcar": orcar,
        "Orcar.search_agent": orcar_search_agent,
        "Orcar.types": orcar_types,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    agent = OrcaLocaAgent(
        model="gateway-model",
        base_url="https://gateway.example/v1",
        config_path=tmp_path / "search.cfg",
    )
    execution = agent._run_upstream(
        "Locate the parser",
        str(tmp_path),
        tmp_path / "repo_manifest.json",
    )

    assert captured["llm"]["api_base"] == "https://gateway.example/v1"
    assert captured["agent"]["llm"].__class__ is FakeOpenAI
    trace_analysis = captured["agent"]["search_input"].trace_analysis_output
    assert isinstance(trace_analysis, FakeTraceAnalysisOutput)
    assert vars(trace_analysis) == {}
    assert captured["content"] == "Locate the parser"
    assert execution.tool_calls == 0


def test_generic_batch_runner_scores_orcaloca_output(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = _write_manifest(repo, tmp_path / "repo_manifest.json")

    def run_search(_problem_statement, _repo_path, _manifest_path):
        return {
            "bug_locations": [
                {
                    "file_path": "src/service.py",
                    "class_name": "Service",
                    "method_name": "run",
                }
            ]
        }

    agent = OrcaLocaAgent(
        model="test-model",
        manifest_path=manifest,
        search_runner=run_search,
    )
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix the service",
        repo_path=str(repo),
        target_files=("src/service.py",),
        target_symbols=("src/service.py:Service.run()",),
    )
    output = tmp_path / "results.jsonl"

    summary = asyncio.run(
        run_baseline_batch(
            [task],
            runner=lambda item: agent.locate_code(**item.to_agent_call()),
            agent_name="orcaloca-codenib",
            model="test-model",
            metrics_k=[1],
            result_path=output,
        )
    )
    row = json.loads(output.read_text(encoding="utf-8"))

    assert summary.succeeded == 1
    assert summary.scored == 1
    assert row["metric_k_files"] == ["src/service.py"]
    assert row["metric_k_node_ids"] == ["src/service.py:Service.run()"]
    assert row["metrics"]["files"]["1"]["recall"] == 1.0
    assert row["metrics"]["symbols"]["1"]["recall"] == 1.0
