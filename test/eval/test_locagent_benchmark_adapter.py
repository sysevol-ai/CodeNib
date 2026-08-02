# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for LocAgent's generic localization benchmark adapter."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import codenib.clients.locagent_agent as locagent_client
from codenib.clients.locagent_agent import LocAgentAgent, LocAgentExecution
from codenib.compiler.manifest import RepoManifest
from codenib.eval.agent_runner.batch import run_baseline_batch
from codenib.eval.baseline import BaselineTask
from codenib.eval.benchmarks.locagent import locagent_regions, parse_locagent_locations
from codenib.integrations._repository import RepositoryEntity
from codenib.source_fingerprint import fingerprint_repository
from codenib.types import NODE_TYPE_CLASS, NODE_TYPE_METHOD


class _FakeRepository:
    files = ("src/model.py", "src/service.py")

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.method = RepositoryEntity(
            vertex_id=1,
            canonical_name="method",
            display_name="src/service.py:BillingService.calculate_tax()",
            kind=NODE_TYPE_METHOD,
            file_path="src/service.py",
            qualified_name="BillingService.calculate_tax",
            simple_name="calculate_tax",
            start_line=1,
            end_line=3,
            class_name="BillingService",
        )
        self.tax_rule = RepositoryEntity(
            vertex_id=2,
            canonical_name="class",
            display_name="src/model.py:TaxRule",
            kind=NODE_TYPE_CLASS,
            file_path="src/model.py",
            qualified_name="TaxRule",
            simple_name="TaxRule",
            start_line=0,
            end_line=1,
        )

    def find_files(self, value):
        return [value] if value in self.files else []

    def find_entities(self, query, *, file_path=None, kinds=None):
        del kinds
        normalized = str(query).removesuffix("()")
        if file_path == "src/service.py" and normalized in {
            "BillingService.calculate_tax",
            "calculate_tax",
        }:
            return [self.method]
        if file_path == "src/model.py" and normalized == "TaxRule":
            return [self.tax_rule]
        return []

    def containing_entity(self, file_path, line):
        if file_path == "src/service.py" and 1 <= line <= 3:
            return self.method
        if file_path == "src/model.py" and 0 <= line <= 1:
            return self.tax_rule
        return None


class _FakeProvider:
    def __init__(self, repo_root: Path) -> None:
        self.repository = _FakeRepository(repo_root)

    def dispatch(self, _name, _arguments):
        return "unused"


def _write_manifest(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "service.py").write_text("pass\n", encoding="utf-8")
    (repo / "src" / "model.py").write_text("pass\n", encoding="utf-8")
    manifest = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo.resolve()),
        source_fingerprint=fingerprint_repository(repo).value,
    ).save(manifest)
    return repo, manifest


def test_adapter_import_does_not_require_locagent_litellm_or_openai() -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import sys
sys.modules["litellm"] = None
sys.modules["openai"] = None
sys.modules["util"] = None
from codenib.clients.locagent_agent import LocAgentAgent
assert LocAgentAgent.__name__ == "LocAgentAgent"
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_parser_handles_ranked_multilingual_location_blocks() -> None:
    output = """
    ```
    /tmp/repo/src/service.py
    lines: 12-18
    class: BillingService
    function: BillingService.calculate_tax

    web/route.ts
    line: 44
    functions: loadRoute, renderRoute

    hallucinated/missing.py
    function: ignore_me
    ```
    """

    locations = parse_locagent_locations(
        output,
        valid_files=("src/service.py", "web/route.ts"),
    )

    assert [location.to_record() for location in locations] == [
        {
            "file_path": "src/service.py",
            "class_name": "BillingService",
            "function_name": "BillingService.calculate_tax",
            "line_start": 12,
            "line_end": 18,
        },
        {
            "file_path": "web/route.ts",
            "class_name": "",
            "function_name": "loadRoute",
            "line_start": 44,
            "line_end": 44,
        },
        {
            "file_path": "web/route.ts",
            "class_name": "",
            "function_name": "renderRoute",
            "line_start": 44,
            "line_end": 44,
        },
    ]
    assert locagent_regions(output, valid_files=("src/service.py", "web/route.ts")) == (
        {"path": "src/service.py", "start": 12, "end": 18},
        {"path": "web/route.ts", "start": 44, "end": 44},
    )


def test_parser_does_not_treat_qualified_symbol_as_file() -> None:
    locations = parse_locagent_locations(
        """
        src/service.py
        function: BillingService.calculate_tax
        """
    )

    assert len(locations) == 1
    assert locations[0].file_path == "src/service.py"
    assert locations[0].function_name == "BillingService.calculate_tax"


def test_protocol_is_extracted_in_an_isolated_python_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "LocAgent"
    runtime = checkout / "util" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "function_calling.py").write_text("", encoding="utf-8")
    python = tmp_path / "locagent-python"
    python.write_text("", encoding="utf-8")
    calls = []
    payload = {
        "system_prompt": "system",
        "task_template": "Inspect {package_name}.\n",
        "pr_template": "Title: {title}\n{description}\n",
        "followup": "verify",
        "tools": [
            {
                "type": "function",
                "function": {"name": "finish", "parameters": {}},
            }
        ],
    }

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    locagent_client._load_upstream_assets.cache_clear()
    monkeypatch.setattr(locagent_client.subprocess, "run", run)
    protocol = locagent_client.load_locagent_protocol(
        checkout,
        problem_statement="Fix parser\nPreserve ranges.",
        package_name="demo",
        python_executable=python,
    )
    locagent_client._load_upstream_assets.cache_clear()

    assert protocol.system_prompt == "system"
    assert protocol.followup == "verify"
    assert "Inspect demo." in protocol.instruction
    assert "Title: Fix parser" in protocol.instruction
    assert "Preserve ranges." in protocol.instruction
    assert protocol.tools[0]["function"]["name"] == "finish"
    assert calls[0][0][0] == str(python.resolve())
    assert calls[0][1]["cwd"] == checkout.resolve()
    assert calls[0][1]["timeout"] == 60


def test_agent_resolves_locagent_output_to_generic_symbols(
    tmp_path: Path,
) -> None:
    repo, manifest = _write_manifest(tmp_path)
    calls = []

    def run_policy(problem_statement, repo_path, provider, context):
        calls.append((problem_statement, repo_path, provider, context))
        return LocAgentExecution(
            final_output="""
            ```
            src/service.py
            line: 2-4
            class: BillingService
            function: calculate_tax

            src/model.py
            class: TaxRule
            ```
            """,
            usage={"prompt_tokens": 120, "completion_tokens": 30},
            tool_calls=3,
            iterations=2,
        )

    agent = LocAgentAgent(
        model="test-model",
        manifest_path=manifest,
        policy_runner=run_policy,
        provider_factory=lambda _manifest: _FakeProvider(repo),
    )
    result = asyncio.run(
        agent.locate_code(
            "Fix tax calculation",
            str(repo),
            {"package_name": "demo"},
        )
    )

    assert result.success is True
    assert [location.symbol_id() for location in result.locations] == [
        "src/service.py:BillingService.calculate_tax()",
        "src/model.py:TaxRule",
    ]
    assert result.locations[0].line_start == 2
    assert result.locations[0].line_end == 4
    assert result.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "tool_calls": 3,
        "iterations": 2,
    }
    assert calls[0][0:2] == ("Fix tax calculation", str(repo.resolve()))
    assert calls[0][3] == {"package_name": "demo"}


def test_agent_rejects_manifest_for_another_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other"
    repo.mkdir()
    other_repo.mkdir()
    manifest = tmp_path / "repo_manifest.json"
    RepoManifest(repo_path=str(other_repo.resolve())).save(manifest)
    called = False

    def run_policy(*_args):
        nonlocal called
        called = True
        return ""

    agent = LocAgentAgent(
        model="test-model",
        manifest_path=manifest,
        policy_runner=run_policy,
    )
    result = asyncio.run(agent.locate_code("Find the bug", str(repo)))

    assert result.success is False
    assert "does not match" in (result.error_message or "")
    assert called is False


def test_default_policy_loop_dispatches_tools_and_finishes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo, manifest = _write_manifest(tmp_path)
    provider = _FakeProvider(repo)
    dispatches = []

    def dispatch(name, arguments):
        dispatches.append((name, dict(arguments)))
        return "indexed evidence"

    provider.dispatch = dispatch
    protocol = locagent_client.LocAgentProtocol(
        system_prompt="system",
        instruction="instruction",
        followup="verify",
        tools=(
            {
                "type": "function",
                "function": {"name": "search_code_snippets", "parameters": {}},
            },
        ),
    )
    monkeypatch.setattr(
        locagent_client,
        "load_locagent_protocol",
        lambda *_args, **_kwargs: protocol,
    )

    def message(content, name, arguments, call_id):
        tool_call = SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments),
            ),
        )
        value = SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=[tool_call],
        )
        value.model_dump = lambda **_kwargs: {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }
        return value

    responses = iter(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=message(
                            "",
                            "search_code_snippets",
                            {"search_terms": ["tax"]},
                            "call-1",
                        )
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=3,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=2),
                ),
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=message(
                            "src/service.py\nfunction: " "BillingService.calculate_tax",
                            "finish",
                            {},
                            "call-2",
                        )
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=15,
                    completion_tokens=5,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=4),
                ),
            ),
        ]
    )

    def create(**_kwargs):
        return next(responses)

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )
    )
    agent = LocAgentAgent(
        model="test-model",
        locagent_checkout=tmp_path / "LocAgent",
        manifest_path=manifest,
        provider_factory=lambda _manifest: provider,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax", str(repo)))

    assert result.success is True
    assert dispatches == [
        ("search_code_snippets", {"search_terms": ["tax"]}),
    ]
    assert result.locations[0].symbol_id() == (
        "src/service.py:BillingService.calculate_tax()"
    )
    assert result.usage is not None
    assert result.usage["prompt_tokens"] == 25
    assert result.usage["completion_tokens"] == 8
    assert result.usage["cached_tokens"] == 6
    assert result.usage["tool_calls"] == 1
    assert result.usage["iterations"] == 2


def test_generic_batch_runner_scores_locagent_output(
    tmp_path: Path,
) -> None:
    repo, manifest = _write_manifest(tmp_path)

    def run_policy(_problem_statement, _repo_path, _provider, _context):
        return """
        src/service.py
        class: BillingService
        function: calculate_tax
        """

    agent = LocAgentAgent(
        model="test-model",
        manifest_path=manifest,
        policy_runner=run_policy,
        provider_factory=lambda _manifest: _FakeProvider(repo),
    )
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix tax calculation",
        repo_path=str(repo),
        target_files=("src/service.py",),
        target_symbols=("src/service.py:BillingService.calculate_tax()",),
    )
    output = tmp_path / "results.jsonl"

    summary = asyncio.run(
        run_baseline_batch(
            [task],
            runner=lambda item: agent.locate_code(**item.to_agent_call()),
            agent_name="locagent-codenib",
            model="test-model",
            metrics_k=[1],
            result_path=output,
        )
    )
    row = json.loads(output.read_text(encoding="utf-8"))

    assert summary.succeeded == 1
    assert summary.scored == 1
    assert row["metric_k_files"] == ["src/service.py"]
    assert row["metric_k_node_ids"] == ["src/service.py:BillingService.calculate_tax()"]
    assert row["metrics"]["files"]["1"]["recall"] == 1.0
    assert row["metrics"]["symbols"]["1"]["recall"] == 1.0
