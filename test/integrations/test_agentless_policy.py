# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from codenib.clients.agentless_agent import AgentlessAgent
from codenib.compiler.manifest import RepoManifest
from codenib.eval.benchmarks.agentless import (
    AgentlessLocation,
    parse_agentless_files,
    parse_agentless_locations,
)


class _Completions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def create(self, **request):
        self.requests.append(request)
        content = self.responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=2,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1),
            ),
        )


class _Client:
    def __init__(self, responses: list[str]) -> None:
        self.chat = SimpleNamespace(completions=_Completions(responses))


class _UnsupportedTemperatureError(Exception):
    body = {
        "error": {
            "message": "Unsupported value for temperature",
            "param": "temperature",
            "code": "unsupported_value",
        }
    }


class _TemperatureRejectingCompletions(_Completions):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.attempts: list[dict] = []
        self.rejected = False

    def create(self, **request):
        self.attempts.append(request)
        if "temperature" in request and not self.rejected:
            self.rejected = True
            raise _UnsupportedTemperatureError
        return super().create(**request)


def test_file_parser_accepts_root_prefixed_ranked_output() -> None:
    result = parse_agentless_files(
        "```\nrepo/src/service.py\n- src/model.py\nmissing.py\n```",
        valid_files=("src/service.py", "src/model.py"),
        root_name="repo",
    )

    assert result == ("src/service.py", "src/model.py")


def test_location_parser_accepts_rendered_markdown_heading() -> None:
    result = parse_agentless_locations(
        "### File: src/service.py ###\nfunction: helper",
        valid_files=("src/service.py",),
        root_name="repo",
    )

    assert result == (
        AgentlessLocation(
            file_path="src/service.py",
            kind="function",
            name="helper",
        ),
    )


def test_location_parser_preserves_agentless_location_types() -> None:
    result = parse_agentless_locations(
        """
        ```
        repo/src/service.py
        function: BillingService.calculate_tax
        variable: TAX_RATE
        line: 4-5

        src/model.py
        class: TaxRule
        ```
        """,
        valid_files=("src/service.py", "src/model.py"),
        root_name="repo",
    )

    assert result == (
        AgentlessLocation(
            file_path="src/service.py",
            kind="function",
            name="BillingService.calculate_tax",
        ),
        AgentlessLocation(
            file_path="src/service.py",
            kind="variable",
            name="TAX_RATE",
        ),
        AgentlessLocation(
            file_path="src/service.py",
            kind="line",
            line_start=4,
            line_end=5,
        ),
        AgentlessLocation(
            file_path="src/model.py",
            kind="class",
            name="TaxRule",
        ),
    )


def test_native_policy_runs_three_stages_over_manifest(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.agentless_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            "```\nrepo/src/service.py\n```",
            "```\nsrc/service.py\nfunction: BillingService.calculate_tax\n```",
            "```\nsrc/service.py\nline: 4\n```",
        ]
    )
    agent = AgentlessAgent(
        model="test-model",
        manifest_path=integration_manifest,
        client_factory=lambda: client,
    )

    result = asyncio.run(
        agent.locate_code(
            "Tax calculations are wrong",
            manifest.repo_path,
        )
    )

    assert result.success is True
    assert len(result.locations) == 1
    location = result.locations[0]
    assert location.file_path == "src/service.py"
    assert location.name == "BillingService.calculate_tax()"
    assert location.type == "method"
    assert location.line_start == 2
    assert location.line_end == 4
    assert result.usage == {
        "prompt_tokens": 30,
        "completion_tokens": 6,
        "cached_tokens": 3,
        "total_tokens": 36,
        "elapsed_seconds": result.usage["elapsed_seconds"],
        "model_calls": 3,
    }
    requests = client.chat.completions.requests
    assert len(requests) == 3
    assert "Repository Structure" in requests[0]["messages"][0]["content"]
    assert "Skeleton of Relevant Files" in requests[1]["messages"][0]["content"]
    assert "4|        return helper(amount)" in requests[2]["messages"][0]["content"]


def test_empty_issue_returns_explicit_failure(integration_manifest: Path) -> None:
    manifest = RepoManifest.load(integration_manifest)
    agent = AgentlessAgent(
        model="test-model",
        manifest_path=integration_manifest,
        client_factory=lambda: _Client([]),
    )

    result = asyncio.run(agent.locate_code("", manifest.repo_path))

    assert result.success is False
    assert result.error_message == "Agentless requires a non-empty problem statement"


def test_policy_retries_without_unsupported_temperature(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.agentless_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    completions = _TemperatureRejectingCompletions(
        [
            "```\nsrc/service.py\n```",
            "```\nsrc/service.py\nfunction: helper\n```",
            "```\nsrc/service.py\nline: 6\n```",
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = AgentlessAgent(
        model="reasoning-model",
        manifest_path=integration_manifest,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix helper", manifest.repo_path))

    assert result.success is True
    assert "temperature" in completions.attempts[0]
    assert all("temperature" not in item for item in completions.attempts[1:])


def test_policy_retries_file_only_symbol_and_line_outputs(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.agentless_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            "src/service.py",
            "src/service.py",
            "src/service.py\nfunction: helper",
            "src/service.py",
            "src/service.py\nline: 6",
        ]
    )
    agent = AgentlessAgent(
        model="test-model",
        manifest_path=integration_manifest,
        max_retries=2,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix helper", manifest.repo_path))

    assert result.success is True
    assert result.usage["model_calls"] == 5
    assert [item["stage"] for item in result.raw_output["trajectory"]] == [
        "file",
        "symbol",
        "symbol",
        "line",
        "line",
    ]


def test_empty_file_stage_records_model_call(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.agentless_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(["missing.py"])
    agent = AgentlessAgent(
        model="test-model",
        manifest_path=integration_manifest,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix missing code", manifest.repo_path))

    assert result.success is True
    assert result.locations == ()
    assert result.usage["model_calls"] == 1


def test_invalid_line_stage_falls_back_to_valid_symbol_stage(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.agentless_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            "repo/src/service.py",
            "src/service.py\nfunction: helper",
            "No usable locations",
        ]
    )
    agent = AgentlessAgent(
        model="test-model",
        manifest_path=integration_manifest,
        max_retries=1,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix helper", manifest.repo_path))

    assert result.success is True
    assert [location.name for location in result.locations] == ["helper()"]
    assert len(client.chat.completions.requests) == 3


def test_invalid_symbol_stage_stops_before_line_context(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.agentless_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            "repo/src/service.py",
            "src/service.py\nfunction: missing_symbol",
        ]
    )
    agent = AgentlessAgent(
        model="test-model",
        manifest_path=integration_manifest,
        max_retries=1,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix unknown code", manifest.repo_path))

    assert result.success is True
    assert [location.file_path for location in result.locations] == ["src/service.py"]
    assert len(client.chat.completions.requests) == 2
